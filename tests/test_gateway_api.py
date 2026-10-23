import asyncio
import json
import os
import socket
import threading
import time
import unittest
from unittest import mock

import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import executor_agent
import gateway
import node_metrics
import phone_hub
import phone_service
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
            level = {"now": "normal"}
            mcp = phone_hub.build_phone_mcp(hub, lambda: level["now"])
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
            level["now"] = "elevated"
            self.assertTrue((await send())["ok"])
            ws.incoming.put_nowait(None)
            await serving

        asyncio.run(scenario())

    def test_wake_unlock_keep_awake_tools(self) -> None:
        async def scenario():
            hub = PhoneHub()
            mcp = phone_hub.build_phone_mcp(hub, lambda: "normal")  # no elevation needed
            ws = FakeSocket("pixel", lambda m: {"ok": True, "result": m["action"]})
            serving = asyncio.create_task(hub.serve(ws))
            await asyncio.sleep(0.01)

            async def use(tool, args):
                result = await mcp.call_tool(tool, args)
                content = result[0] if isinstance(result, tuple) else result
                return json.loads(content[0].text)

            self.assertEqual(await use("phone_wake", {}), {"ok": True, "result": "wake"})
            self.assertEqual(await use("phone_unlock", {}), {"ok": True, "result": "unlock"})
            self.assertTrue((await use("phone_keep_awake", {}))["ok"])
            self.assertTrue((await use("phone_keep_awake", {"seconds": 0}))["ok"])
            sent = [(m["action"], {k: v for k, v in m["args"].items() if k != "deny_packages"})
                    for m in ws.sent]
            self.assertEqual(sent, [("wake", {}), ("unlock", {}), ("keep_awake", {"seconds": 300}),
                                    ("keep_awake", {"seconds": 0})])
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
        outcome = {"short": "OK: 12% used", "detail": "OK: 12% used\n\n/ 12%",
                   "steps": [{"tool": "cluster.run_command", "args": {"command": "df -h"},
                              "ok": True, "result": "..."}],
                   "needs_confirmation": False}
        with mock.patch.object(gateway, "dispatch", mock.AsyncMock(return_value=outcome)) as dispatch:
            reply = self.client.post("/command", json={"text": "диск", "device_id": "pixel"}).json()
        self.assertEqual(dispatch.await_args.args[0::2], ("диск", "pixel"))
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

    def test_task_goes_to_the_executor_agent(self) -> None:
        sent = []

        def agent(request):
            sent.append(request)
            if request.url.path == "/task":
                return gateway.httpx.Response(200, json={"short": "OK", "detail": "OK", "steps": [],
                                                         "needs_confirmation": False})
            return gateway.httpx.Response(502, json={"detail": "claude exited with 1"})

        real_client = gateway.httpx.AsyncClient
        fake = lambda **kw: real_client(transport=gateway.httpx.MockTransport(agent), **kw)
        with mock.patch.object(gateway.httpx, "AsyncClient", fake):
            result = asyncio.run(gateway.dispatch("uptime", "r1", "pixel"))
            self.assertEqual(result["short"], "OK")
            self.assertEqual(str(sent[0].url), f"{gateway.AGENTS['executor']}/task")
            self.assertEqual(json.loads(sent[0].content),
                             {"text": "uptime", "request_id": "r1", "device_id": "pixel"})
            with mock.patch.dict(gateway.AGENTS, {"executor": "http://broken"}):
                sent.clear()
                agent_failing = lambda r: gateway.httpx.Response(502, json={"detail": "claude exited with 1"})
                fake2 = lambda **kw: real_client(transport=gateway.httpx.MockTransport(agent_failing), **kw)
                with mock.patch.object(gateway.httpx, "AsyncClient", fake2):
                    with self.assertRaisesRegex(RuntimeError, "executor: claude exited with 1"):
                        asyncio.run(gateway.dispatch("uptime", "r2", "pixel"))

    def test_monitor_without_services(self) -> None:
        with mock.patch.object(gateway, "service_get", mock.AsyncMock(side_effect=ConnectionError("down"))):
            snap = self.client.get("/monitor?remote=false").json()
        self.assertFalse(snap["nodes"][0]["ok"])
        self.assertEqual((snap["gateway"]["phones"], snap["gateway"]["agents"]), ({}, ["executor"]))

    def test_monitor_collects_from_services(self) -> None:
        answers = {
            f"{gateway.AGENTS['executor']}/metrics?remote=false": [{"node": "local", "ok": True}],
            f"{gateway.PHONE_URL}/phones": {"pixel": {"connected": True}},
            f"{gateway.AGENTS['executor']}/info": {"model": "claude-haiku-5-5", "mcp_servers": ["cluster", "phone"]},
        }
        with mock.patch.object(gateway, "service_get",
                               mock.AsyncMock(side_effect=lambda url, timeout=12: answers[url])):
            snap = self.client.get("/monitor?remote=false").json()
        self.assertTrue(snap["nodes"][0]["ok"])
        self.assertEqual(snap["gateway"]["phones"], {"pixel": {"connected": True}})
        self.assertEqual(snap["gateway"]["model"], "claude-haiku-5-5")

    def test_tools_come_from_the_agents(self) -> None:
        tools = [{"server": "phone", "name": "read_screen", "description": ""}]
        with mock.patch.object(gateway, "service_get", mock.AsyncMock(return_value={"tools": tools})):
            self.assertEqual(self.client.get("/agent/tools").json()["tools"], tools)
        with mock.patch.object(gateway, "service_get", mock.AsyncMock(side_effect=ConnectionError())):
            self.assertEqual(self.client.get("/agent/tools").json()["tools"], [])


    def test_request_duration_metric(self) -> None:
        def count(channel: str, outcome: str) -> float:
            return gateway.REGISTRY.get_sample_value(
                "geba_request_duration_seconds_count",
                {"channel": channel, "outcome": outcome}) or 0.0

        before = {key: count(*key) for key in
                  [("text", "ok"), ("text", "error"), ("voice", "ok"), ("voice", "error")]}

        ok = {"short": "OK: up", "detail": "OK: up", "steps": [], "needs_confirmation": False}
        with mock.patch.object(gateway, "dispatch", mock.AsyncMock(return_value=ok)):
            self.client.post("/command", json={"text": "uptime", "device_id": "pixel"})
        self.assertEqual(count("text", "ok"), before["text", "ok"] + 1)

        with mock.patch.object(gateway, "dispatch",
                               mock.AsyncMock(side_effect=RuntimeError("executor: down"))):
            reply = self.client.post("/command", json={"text": "uptime"}).json()
        self.assertTrue(reply["result"].startswith("ERROR"))
        self.assertEqual(count("text", "error"), before["text", "error"] + 1)

        # Refused before processing: not observed at all.
        self.assertEqual(self.client.post("/command", json={"text": "  "}).status_code, 400)
        self.assertEqual(self.client.post("/voice", content=b"\0" * (gateway.MAX_VOICE_BYTES + 2),
                                          headers={"content-type": "application/octet-stream"}
                                          ).status_code, 413)
        self.assertEqual(count("text", "ok"), before["text", "ok"] + 1)
        self.assertEqual(count("text", "error"), before["text", "error"] + 1)
        self.assertEqual(count("voice", "error"), before["voice", "error"])

        # STT failing is a voice error.
        with mock.patch.object(gateway, "transcribe_audio",
                               mock.AsyncMock(side_effect=ConnectionError("whisper down"))):
            self.assertEqual(self.client.post("/voice", content=b"\0" * 3200).status_code, 500)
        self.assertEqual(count("voice", "error"), before["voice", "error"] + 1)

        with mock.patch.object(gateway, "transcribe_audio", mock.AsyncMock(return_value="uptime")), \
                mock.patch.object(gateway, "dispatch", mock.AsyncMock(return_value=ok)):
            self.client.post("/command/audio", content=b"\0" * 3200)
        self.assertEqual(count("voice", "ok"), before["voice", "ok"] + 1)


class InternalServerTests(unittest.TestCase):
    def test_elevation_read_and_request(self) -> None:
        with mock.patch.object(gateway, "elevation", Elevation()):
            client = TestClient(gateway.build_internal_app())
            self.assertEqual(client.get("/internal/elevation").json()["level"], "normal")
            asked = client.post("/internal/elevation/request", json={
                "level": "unrestricted", "minutes": 5, "reason": "apt", "device_id": "pixel"}).json()
            self.assertTrue(asked["pending"])
            status = client.get("/internal/elevation").json()
            # Asking lifts nothing: only the phone grants it.
            self.assertEqual((status["level"], status["pending"][0]["code"]), ("normal", asked["code"]))
            bad = client.post("/internal/elevation/request", json={"level": "root", "minutes": 5}).json()
            self.assertFalse(bad["ok"])

    def test_metrics_only_on_the_metrics_server(self) -> None:
        metrics = TestClient(gateway.build_metrics_app())
        response = metrics.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("geba_request_duration_seconds", response.text)
        # Nothing else there, the elevation endpoints included.
        self.assertEqual(metrics.get("/internal/elevation").status_code, 404)
        self.assertEqual(metrics.post("/internal/elevation/request",
                                      json={"level": "elevated", "minutes": 5}).status_code, 404)
        self.assertEqual(metrics.get("/docs").status_code, 404)
        self.assertEqual(metrics.get("/openapi.json").status_code, 404)
        self.assertEqual(TestClient(gateway.build_internal_app()).get("/metrics").status_code, 404)
        self.assertEqual(TestClient(gateway.app).get("/metrics").status_code, 404)

    def test_internal_server_refuses_denied_subnets(self) -> None:
        with mock.patch.object(gateway, "elevation", Elevation()), \
                mock.patch.dict(os.environ, {"INTERNAL_DENY_SUBNETS": "172.31.250.0/28, 10.9.0.0/16"}):
            internal = gateway.build_internal_app()
            prometheus = TestClient(internal, client=("172.31.250.2", 40000))
            self.assertEqual(prometheus.get("/internal/elevation").status_code, 403)
            refused = prometheus.post("/internal/elevation/request",
                                      json={"level": "elevated", "minutes": 5})
            self.assertEqual(refused.status_code, 403)
            self.assertEqual(gateway.elevation.status()["pending"], [])
            self.assertEqual(TestClient(internal, client=("10.9.3.4", 1)).get(
                "/internal/elevation").status_code, 403)
            # Other networks (executor, phone, host loopback) still work.
            for host in ("172.20.0.3", "172.31.250.16", "127.0.0.1"):
                self.assertEqual(TestClient(internal, client=(host, 40000)).get(
                    "/internal/elevation").status_code, 200, host)

        with mock.patch.dict(os.environ, {"INTERNAL_DENY_SUBNETS": ""}):
            open_app = gateway.build_internal_app()
        self.assertEqual(TestClient(open_app, client=("172.31.250.2", 1)).get(
            "/internal/elevation").status_code, 200)

    def test_client_denied_helper(self) -> None:
        denied = gateway.parse_subnets("172.31.250.0/28")
        self.assertTrue(gateway.client_denied("172.31.250.1", denied))
        self.assertTrue(gateway.client_denied("::ffff:172.31.250.5", denied))
        self.assertFalse(gateway.client_denied("172.31.250.17", denied))
        self.assertFalse(gateway.client_denied(None, denied))
        self.assertFalse(gateway.client_denied("172.31.250.1", []))
        self.assertEqual(gateway.parse_subnets(" , "), [])
        with self.assertRaises(ValueError):
            gateway.parse_subnets("not-a-subnet")


class ExecutorAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(executor_agent.app)

    def test_task_runs_claude(self) -> None:
        outcome = CommandOutcome("OK: up", "OK: up\n\n3 days", [{"tool": "cluster.run_command"}])
        with mock.patch.object(executor_agent, "handle_text_command",
                               mock.AsyncMock(return_value=outcome)) as claude:
            reply = self.client.post("/task", json={"text": "uptime", "request_id": "r1",
                                                    "device_id": "pixel"}).json()
        self.assertEqual(claude.await_args.args, ("uptime", "pixel"))
        self.assertEqual(reply, {"short": "OK: up", "detail": "OK: up\n\n3 days",
                                 "steps": [{"tool": "cluster.run_command"}], "needs_confirmation": False})

    def test_task_failure_is_502(self) -> None:
        with mock.patch.object(executor_agent, "handle_text_command",
                               mock.AsyncMock(side_effect=RuntimeError("not logged in"))):
            response = self.client.post("/task", json={"text": "uptime"})
        self.assertEqual((response.status_code, response.json()["detail"]), (502, "not logged in"))

    def test_tools_list_cluster_and_phone(self) -> None:
        phone_tools = asyncio.run(phone_hub.build_phone_mcp(PhoneHub(), lambda: "normal").list_tools())
        with mock.patch.object(executor_agent, "phone_tools", mock.AsyncMock(return_value=phone_tools)):
            names = {(t["server"], t["name"]) for t in self.client.get("/tools").json()["tools"]}
        self.assertIn(("cluster", "run_command"), names)
        self.assertIn(("cluster", "request_elevation"), names)
        self.assertIn(("phone", "read_screen"), names)
        with mock.patch.object(executor_agent, "phone_tools", mock.AsyncMock(side_effect=ConnectionError())):
            names = {t["server"] for t in self.client.get("/tools").json()["tools"]}
        self.assertEqual(names, {"cluster"})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PhoneServiceTests(unittest.TestCase):
    def test_phone_connects_and_is_listed(self) -> None:
        # No `with`: the MCP session manager's lifespan may run only once per
        # process, and the relay test below needs it.
        client = TestClient(phone_service.app)
        with client.websocket_connect("/phone/ws") as ws:
            ws.send_text(json.dumps({"type": "hello", "device_id": "pixel-test"}))
            for _ in range(50):
                if "pixel-test" in phone_service.hub.connected():
                    break
                time.sleep(0.01)
            self.assertIn("pixel-test", client.get("/health").json()["connected"])
        self.assertIn("pixel-test", client.get("/phones").json())

    def test_gateway_relays_the_phone_socket(self) -> None:
        """Real phone service on a port, the gateway's /phone/ws in front."""
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(phone_service.app, host="127.0.0.1", port=port,
                                               log_level="warning"))
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_until_complete, args=(server.serve(),), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(setattr, server, "should_exit", True)
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.01)

        hub = phone_service.hub
        with mock.patch.object(gateway, "PHONE_WS_URL", f"ws://127.0.0.1:{port}/phone/ws"), \
                TestClient(gateway.app).websocket_connect("/phone/ws") as ws:
            ws.send_text(json.dumps({"type": "hello", "device_id": "pixel-relay"}))
            for _ in range(200):
                if "pixel-relay" in hub.connected():
                    break
                time.sleep(0.01)
            self.assertIn("pixel-relay", hub.connected())

            call = asyncio.run_coroutine_threadsafe(hub.call("status", device_id="pixel-relay", timeout=5), loop)
            action = json.loads(ws.receive_text())
            self.assertEqual(action["action"], "status")
            ws.send_text(json.dumps({"type": "result", "id": action["id"], "ok": True,
                                     "result": {"battery": 55}}))
            self.assertEqual(call.result(5), {"battery": 55})

        for _ in range(200):
            if "pixel-relay" not in hub.connected():
                break
            time.sleep(0.01)
        self.assertNotIn("pixel-relay", hub.connected())


class NodeMetricsTests(unittest.TestCase):
    def test_collects_this_machine(self) -> None:
        node = node_metrics.collect(units=[])
        self.assertTrue(node["ok"])
        self.assertGreater(node["memory"]["total_mb"], 0)
        self.assertGreater(node["cpu_count"], 0)
        self.assertEqual(len(node["load"]), 3)


if __name__ == "__main__":
    unittest.main()
