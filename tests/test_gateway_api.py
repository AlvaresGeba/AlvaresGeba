import asyncio
import json
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import gateway
import node_metrics
import phone_hub
from command_router import CommandOutcome
from elevation import CHALLENGE_TTL_S, Elevation, ElevationError
from phone_hub import PhoneError, PhoneHub
from tests.test_voice import use_temp_history


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ElevationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.e = Elevation(clock=self.clock)

    def test_voice_request_needs_phone_confirmation(self) -> None:
        challenge = self.e.request("unrestricted", 300, "deploy", "watch-01")
        self.assertEqual(self.e.level(), "normal")
        self.assertEqual(len(self.e.status()["pending"]), 1)

        self.e.confirm(challenge.id, None, "phone")

        status = self.e.status()
        self.assertEqual((status["level"], status["remaining_s"], status["pending"]),
                         ("unrestricted", 300, []))

    def test_lift_expires_and_revokes(self) -> None:
        self.e.lift("elevated", 60, "", "pixel")
        self.assertEqual(self.e.level(), "elevated")
        self.clock.now += 61
        self.assertEqual(self.e.level(), "normal")

        self.e.lift("elevated", 60, "", "pixel")
        self.e.revoke()
        self.assertEqual(self.e.level(), "normal")

    def test_challenge_expires(self) -> None:
        challenge = self.e.request("elevated", 60, "", "watch-01")
        self.clock.now += CHALLENGE_TTL_S + 1
        with self.assertRaises(ElevationError):
            self.e.confirm(challenge.id, None, "phone")

    def test_pin(self) -> None:
        challenge = self.e.request("elevated", 60, "", "watch-01")
        with mock.patch("elevation.ELEVATION_PIN", "4321"):
            self.assertTrue(self.e.status()["pin_required"])
            with self.assertRaises(ElevationError):
                self.e.confirm(challenge.id, "0000", "phone")
            self.e.confirm(challenge.id, "4321", "phone")
        self.assertEqual(self.e.level(), "elevated")

    def test_bad_level_and_ttl_cap(self) -> None:
        with self.assertRaises(ElevationError):
            self.e.lift("root", 60, "", "pixel")
        _, ttl = self.e.lift("elevated", 10**6, "", "pixel")
        self.assertLessEqual(ttl, 3600)


class FakeSocket:
    """Plays the phone side of /phone/ws for PhoneHub.serve()."""

    def __init__(self, device_id: str, answer) -> None:
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict] = []
        self.answer = answer
        self.incoming.put_nowait(json.dumps({"type": "hello", "device_id": device_id}))

    async def accept(self) -> None:
        pass

    async def receive_text(self) -> str:
        msg = await self.incoming.get()
        if msg is None:
            raise WebSocketDisconnect()
        return msg

    async def send_text(self, text: str) -> None:
        msg = json.loads(text)
        self.sent.append(msg)
        reply = self.answer(msg)
        if reply is not None:
            self.incoming.put_nowait(json.dumps({"type": "result", "id": msg["id"], **reply}))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.incoming.put_nowait(None)


class PhoneHubTests(unittest.TestCase):
    def test_action_round_trip(self) -> None:
        async def scenario():
            hub = PhoneHub()
            ws = FakeSocket("pixel", lambda m: {"ok": True, "result": {"battery": 80}})
            serving = asyncio.create_task(hub.serve(ws))
            await asyncio.sleep(0.01)
            self.assertEqual(hub.connected(), ["pixel"])

            result = await hub.call("status")
            self.assertEqual(result, {"battery": 80})
            self.assertEqual(ws.sent[0]["action"], "status")
            self.assertEqual(ws.sent[0]["args"]["deny_packages"], phone_hub.DENY_PACKAGES)

            ws.incoming.put_nowait(None)
            await serving
            self.assertEqual(hub.connected(), [])
            self.assertIn("pixel", hub.links())

        asyncio.run(scenario())

    def test_failures(self) -> None:
        async def scenario():
            hub = PhoneHub()
            with self.assertRaisesRegex(PhoneError, "No phone"):
                await hub.call("status")

            ws = FakeSocket("pixel", lambda m: {"ok": False, "error": "App kz.kaspi.mobile is protected"}
                            if m["action"] == "open_app" else None)
            serving = asyncio.create_task(hub.serve(ws))
            await asyncio.sleep(0.01)
            with self.assertRaisesRegex(PhoneError, "protected"):
                await hub.call("open_app", {"app": "Kaspi"})
            with self.assertRaisesRegex(PhoneError, "did not answer"):
                await hub.call("status", timeout=0.05)
            ws.incoming.put_nowait(None)
            await serving

        asyncio.run(scenario())

    def test_sms_needs_confirmation_unless_elevated(self) -> None:
        async def scenario():
            hub = PhoneHub()
            elevation = Elevation()
            mcp = phone_hub.build_phone_mcp(hub, elevation, {})
            ws = FakeSocket("pixel", lambda m: {"ok": True, "result": "sms sent"})
            serving = asyncio.create_task(hub.serve(ws))
            await asyncio.sleep(0.01)

            async def send(**kw):
                result = await mcp.call_tool("send_sms", {"number": "+7700", "text": "hi", **kw})
                content = result[0] if isinstance(result, tuple) else result  # (content, structured)
                return json.loads(content[0].text)

            self.assertTrue((await send())["needs_confirmation"])
            self.assertEqual(ws.sent, [])
            self.assertTrue((await send(confirm=True))["ok"])
            elevation.lift("elevated", 60, "", "pixel")
            self.assertTrue((await send())["ok"])
            ws.incoming.put_nowait(None)
            await serving

        asyncio.run(scenario())


class GatewayApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.history = use_temp_history(self)
        patcher = mock.patch.object(gateway, "elevation", Elevation())
        self.elevation = patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(gateway.app)

    def test_elevation_endpoints(self) -> None:
        self.assertEqual(self.client.get("/elevation").json()["level"], "normal")

        lifted = self.client.post("/elevation/request", json={
            "level": "elevated", "ttl_s": 600, "reason": "maintenance", "device_id": "pixel"}).json()
        self.assertEqual(lifted, {"ok": True, "level": "elevated", "ttl_s": 600})
        status = self.client.get("/elevation").json()
        self.assertEqual((status["level"], status["reason"]), ("elevated", "maintenance"))

        self.client.post("/elevation/revoke", json={})
        self.assertEqual(self.client.get("/elevation").json()["level"], "normal")

        challenge = self.elevation.request("unrestricted", 120, "voice", "watch-01")
        pending = self.client.get("/elevation").json()["pending"]
        self.assertEqual([p["code"] for p in pending], [challenge.code])
        self.assertEqual(self.client.post("/elevation/confirm", json={"id": "nope"}).json()["ok"], False)
        confirmed = self.client.post("/elevation/confirm", json={"id": challenge.id}).json()
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertEqual(self.client.get("/elevation").json()["level"], "unrestricted")

        other = self.elevation.request("elevated", 120, "", "watch-01")
        self.client.post("/elevation/deny", json={"id": other.id})
        self.assertEqual(self.client.get("/elevation").json()["pending"], [])

    def test_command_history_and_logs(self) -> None:
        outcome = CommandOutcome("OK: 12% used", "OK: 12% used\n\n/ 12%",
                                 [{"tool": "cluster.run_command", "args": {"command": "df -h"},
                                   "ok": True, "result": "..."}])
        with mock.patch.object(gateway, "handle_text_command", mock.AsyncMock(return_value=outcome)):
            reply = self.client.post("/command", json={"text": "диск", "device_id": "pixel"}).json()
        self.assertEqual((reply["result"], len(reply["steps"])), ("OK: 12% used", 1))

        [entry] = self.client.get("/history/recent?q=диск").json()["requests"]
        self.assertEqual((entry["ok"], entry["step_count"], entry["channel"]), (1, 1, "text"))
        self.assertEqual(self.client.get(f"/history/{entry['id']}").json()["steps"][0]["tool"],
                         "cluster.run_command")
        stats = self.client.get("/history/stats?hours=1").json()
        self.assertEqual((stats["total"], stats["failed"]), (1, 0))

        logs = self.client.get("/logs/recent?q=диск&level=INFO").json()["entries"]
        self.assertTrue(any("диск" in e["msg"] for e in logs))
        self.assertFalse(self.client.get("/logs/recent?level=CRITICAL&q=диск").json()["entries"])

    def test_monitor_without_executor(self) -> None:
        with mock.patch.object(gateway, "executor_session_call",
                               mock.AsyncMock(side_effect=ConnectionError("down"))):
            snap = self.client.get("/monitor?remote=false").json()
        self.assertFalse(snap["nodes"][0]["ok"])
        self.assertEqual(snap["gateway"]["mcp_servers"], ["cluster", "phone"])

    def test_tools_include_phone(self) -> None:
        with mock.patch.object(gateway, "executor_session_call",
                               mock.AsyncMock(side_effect=ConnectionError("down"))):
            tools = self.client.get("/agent/tools").json()["tools"]
        names = {(t["server"], t["name"]) for t in tools}
        self.assertIn(("phone", "read_screen"), names)
        self.assertIn(("phone", "request_elevation"), names)

    def test_phone_websocket(self) -> None:
        with self.client.websocket_connect("/phone/ws") as ws:
            ws.send_text(json.dumps({"type": "hello", "device_id": "pixel-test"}))
            for _ in range(50):
                if "pixel-test" in gateway.phones.connected():
                    break
                time.sleep(0.01)
            self.assertIn("pixel-test", gateway.phones.connected())
        snap_phones = gateway.phones.links()
        self.assertIn("pixel-test", snap_phones)


class InternalServerTests(unittest.TestCase):
    def test_elevation_readable_mcp_loopback_only(self) -> None:
        with TestClient(gateway.build_internal_app()) as client:  # client host = "testclient"
            self.assertEqual(client.get("/internal/elevation").json(), {"level": "normal"})
            self.assertEqual(client.post("/mcp", json={}).status_code, 403)


class NodeMetricsTests(unittest.TestCase):
    def test_collects_this_machine(self) -> None:
        node = node_metrics.collect(units=[])
        self.assertTrue(node["ok"])
        self.assertGreater(node["memory"]["total_mb"], 0)
        self.assertGreater(node["cpu_count"], 0)
        self.assertEqual(len(node["load"]), 3)


if __name__ == "__main__":
    unittest.main()
