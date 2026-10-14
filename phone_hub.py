"""
phone_hub.py

The phone as a set of tools for Claude.

The app (PhoneAgentService) keeps a WebSocket open to gateway /phone/ws,
with the same mTLS client certificate as every other call:

    phone  -> server  {"type": "hello", "device_id": "pixel-10a"}
    server -> phone   {"type": "action", "id": "...", "action": "tap",
                       "args": {..., "deny_packages": [...]}}
    phone  -> server  {"type": "result", "id": "...", "ok": true, "result": ...}
                      {"type": "result", "id": "...", "ok": false, "error": "..."}

PhoneHub tracks the connected phones and turns an action into an awaitable
call. build_phone_mcp() wraps it as the "phone" MCP server that Claude
Code gets next to the executor's "cluster" server. It is served only on
127.0.0.1 inside the orchestrator (see gateway.py) - the phone is driven by
Claude, never directly by anything on the docker networks.

The phone has its own hardcoded app deny-list (PhonePolicy) that the
server can only add to; PHONE_DENY_PACKAGES is that addition.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from elevation import Elevation, ElevationError

logger = logging.getLogger("phone_hub")

ACTION_TIMEOUT_S = 20.0  # the phone gives up on its side after 18 s

# Banks, payments and 2FA - never driven, on any elevation level. Added to
# the phone's own list, which already covers settings, the store, Kaspi,
# Google Wallet / Authenticator and the app itself.
DEFAULT_DENY_PACKAGES = [
    "com.authy.authy",
    "com.azure.authenticator",
    "com.lastpass.lpandroid",
    "com.x8bit.bitwarden",
    "com.agilebits.onepassword",
    "kz.kaspi.business",
    "kz.halyk.onlinebank",
    "kz.homebank.mobile",
    "kz.bcc.starbanking",
    "kz.jusan.mobile",
    "kz.forte.bank",
    "com.paypal.android.p2pmobile",
]
DENY_PACKAGES = [p.strip() for p in os.environ.get(
    "PHONE_DENY_PACKAGES", ",".join(DEFAULT_DENY_PACKAGES)).split(",") if p.strip()]

# A disconnected phone is still listed (with its last-seen age) this long.
FORGET_AFTER_S = 3600


class PhoneError(RuntimeError):
    pass


@dataclass
class _Phone:
    device_id: str
    ws: WebSocket | None
    connected_at: float
    last_seen: float
    pending: dict[str, asyncio.Future] = field(default_factory=dict)


class PhoneHub:
    def __init__(self) -> None:
        self._phones: dict[str, _Phone] = {}

    # -- connection -----------------------------------------------------

    async def serve(self, ws: WebSocket) -> None:
        """Runs one phone's WebSocket for as long as it stays open."""
        await ws.accept()
        phone: _Phone | None = None
        try:
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=15))
            if hello.get("type") != "hello" or not str(hello.get("device_id", "")).strip():
                await ws.close(code=1008, reason="expected hello")
                return
            device_id = str(hello["device_id"]).strip()[:64]
            old = self._phones.get(device_id)
            if old is not None and old.ws is not None:
                # Same phone reconnected (network change): the old socket is dead.
                self._fail_pending(old, "phone reconnected")
                try:
                    await old.ws.close(code=1012)
                except Exception:  # noqa: BLE001
                    pass
            now = time.time()
            phone = _Phone(device_id, ws, now, now)
            self._phones[device_id] = phone
            logger.info("Phone connected: %s", device_id)

            while True:
                msg = json.loads(await ws.receive_text())
                phone.last_seen = time.time()
                if msg.get("type") != "result":
                    continue
                fut = phone.pending.pop(str(msg.get("id")), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except (WebSocketDisconnect, asyncio.TimeoutError, json.JSONDecodeError):
            pass
        except Exception:  # noqa: BLE001
            logger.exception("Phone socket failed")
        finally:
            if phone is not None and self._phones.get(phone.device_id) is phone:
                phone.ws = None
                phone.last_seen = time.time()
                self._fail_pending(phone, "phone disconnected")
                logger.info("Phone disconnected: %s", phone.device_id)

    @staticmethod
    def _fail_pending(phone: _Phone, why: str) -> None:
        for fut in phone.pending.values():
            if not fut.done():
                fut.set_exception(PhoneError(why))
        phone.pending.clear()

    # -- state ----------------------------------------------------------

    def connected(self) -> list[str]:
        return [p.device_id for p in self._phones.values() if p.ws is not None]

    def links(self) -> dict[str, dict]:
        """For /monitor: {device_id: {"last_seen_s_ago": s}} - 0 while connected."""
        now = time.time()
        for device_id in [d for d, p in self._phones.items()
                          if p.ws is None and now - p.last_seen > FORGET_AFTER_S]:
            del self._phones[device_id]
        return {
            p.device_id: {"last_seen_s_ago": 0.0 if p.ws is not None else round(now - p.last_seen, 1),
                          "connected": p.ws is not None}
            for p in self._phones.values()
        }

    def _pick(self, device_id: str | None) -> _Phone:
        if device_id:
            phone = self._phones.get(device_id)
            if phone is None or phone.ws is None:
                raise PhoneError(f"Phone '{device_id}' is not connected")
            return phone
        live = [p for p in self._phones.values() if p.ws is not None]
        if not live:
            raise PhoneError("No phone is connected to the gateway")
        return max(live, key=lambda p: p.connected_at)

    # -- calls ----------------------------------------------------------

    async def call(self, action: str, args: dict | None = None, device_id: str | None = None,
                   timeout: float = ACTION_TIMEOUT_S) -> Any:
        phone = self._pick(device_id)
        call_id = secrets.token_hex(6)
        payload = dict(args or {})
        payload["deny_packages"] = DENY_PACKAGES
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        phone.pending[call_id] = fut
        try:
            await phone.ws.send_text(json.dumps(
                {"type": "action", "id": call_id, "action": action, "args": payload},
                ensure_ascii=False))
            reply = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise PhoneError(f"Phone did not answer '{action}' within {timeout:.0f}s") from None
        finally:
            phone.pending.pop(call_id, None)
        if not reply.get("ok"):
            raise PhoneError(reply.get("error") or "action failed")
        return reply.get("result")


# ---------------------------------------------------------------------------
# The "phone" MCP server
# ---------------------------------------------------------------------------

def build_phone_mcp(hub: PhoneHub, elevation: Elevation, requests: dict[str, dict]) -> FastMCP:
    """`requests` maps the X-Request-Id header (set per command in the MCP
    config, see command_router.py) to {"device_id": ...} - which device the
    command came from, for request_elevation."""

    mcp = FastMCP(
        "phone",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*"],
        ),
    )

    def origin(ctx: Context) -> dict:
        request = getattr(ctx.request_context, "request", None)
        rid = request.headers.get("x-request-id", "") if request is not None else ""
        return requests.get(rid, {})

    async def run(action: str, args: dict | None = None, device_id: str | None = None) -> dict:
        logger.info("phone action %s %s", action, args or {})
        try:
            result = await hub.call(action, args, device_id or None)
        except PhoneError as exc:
            logger.info("phone action %s failed: %s", action, exc)
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "result": result}

    def drop_none(**kw) -> dict:
        return {k: v for k, v in kw.items() if v is not None}

    @mcp.tool()
    async def phone_status(device_id: str = "") -> dict:
        """Battery, charging, screen on/locked, foreground app, volume, DND,
        and whether accessibility / notification access are granted. Cheap -
        call it first when unsure what state the phone is in.
        device_id: which phone; empty = the most recently connected one."""
        return await run("status", None, device_id)

    @mcp.tool()
    async def read_screen(max_nodes: int = 80, device_id: str = "") -> dict:
        """What is on the phone screen: {"package", "nodes": [{"id", "text",
        "desc", "cls", "click", "edit", "scroll", "rid", "b": [l,t,r,b]}]}.
        Node ids are only valid until the screen changes - read again after
        tapping or typing."""
        return await run("read_screen", {"max_nodes": max_nodes}, device_id)

    @mcp.tool()
    async def tap(node_id: int | None = None, text: str | None = None, x: int | None = None,
                  y: int | None = None, long_press: bool = False, device_id: str = "") -> dict:
        """Tap a node from read_screen (node_id), a visible text, or x/y
        screen coordinates. long_press for a long press."""
        return await run("tap", drop_none(node_id=node_id, text=text, x=x, y=y,
                                          long_press=long_press), device_id)

    @mcp.tool()
    async def type_text(text: str, node_id: int | None = None, submit: bool = False,
                        device_id: str = "") -> dict:
        """Type into the focused field, or into node_id. submit=true presses
        enter/search afterwards."""
        return await run("type_text", drop_none(text=text, node_id=node_id, submit=submit), device_id)

    @mcp.tool()
    async def scroll(direction: str = "down", device_id: str = "") -> dict:
        """Scroll the current screen: up, down, left or right."""
        return await run("scroll", {"direction": direction}, device_id)

    @mcp.tool()
    async def press_key(key: str, device_id: str = "") -> dict:
        """System key: back, home, recents, notifications, quick_settings,
        power_dialog, lock_screen."""
        return await run("global", {"key": key}, device_id)

    @mcp.tool()
    async def list_apps(query: str = "", device_id: str = "") -> dict:
        """Installed launcher apps [{"label", "package"}], filtered by query."""
        return await run("list_apps", {"query": query}, device_id)

    @mcp.tool()
    async def open_app(app: str, device_id: str = "") -> dict:
        """Open an app by label ("Telegram") or package name."""
        return await run("open_app", {"app": app}, device_id)

    @mcp.tool()
    async def open_url(url: str, device_id: str = "") -> dict:
        """Open a URL (browser or the app that handles it)."""
        return await run("open_url", {"url": url}, device_id)

    @mcp.tool()
    async def set_alarm(hour: int, minute: int, label: str = "", device_id: str = "") -> dict:
        """Set an alarm (24h clock)."""
        return await run("set_alarm", {"hour": hour, "minute": minute, "label": label}, device_id)

    @mcp.tool()
    async def set_timer(seconds: int, label: str = "", device_id: str = "") -> dict:
        """Start a countdown timer."""
        return await run("set_timer", {"seconds": seconds, "label": label}, device_id)

    @mcp.tool()
    async def flashlight(on: bool, device_id: str = "") -> dict:
        """Turn the flashlight on or off."""
        return await run("flashlight", {"on": on}, device_id)

    @mcp.tool()
    async def volume(level: int, stream: str = "media", device_id: str = "") -> dict:
        """Set volume in percent (0-100) for media, ring, alarm or notification."""
        return await run("volume", {"level": level, "stream": stream}, device_id)

    @mcp.tool()
    async def do_not_disturb(on: bool, device_id: str = "") -> dict:
        """Turn Do Not Disturb on or off."""
        return await run("dnd", {"on": on}, device_id)

    @mcp.tool()
    async def media(command: str = "play_pause", device_id: str = "") -> dict:
        """Media key: play_pause, next or previous."""
        return await run("media", {"command": command}, device_id)

    @mcp.tool()
    async def notifications(limit: int = 10, device_id: str = "") -> dict:
        """Latest notifications [{"app", "pkg", "title", "text", "time"}]."""
        return await run("notifications", {"limit": limit}, device_id)

    @mcp.tool()
    async def send_sms(number: str, text: str, confirm: bool = False, device_id: str = "") -> dict:
        """Send an SMS. Needs confirm=true - set it ONLY if the user clearly
        confirmed sending this exact message to this number (unless
        restrictions are lifted)."""
        if not confirm and elevation.level() == "normal":
            return {"ok": False, "needs_confirmation": True,
                    "error": f"Sending an SMS to {number} requires confirmation. "
                             "Call again with confirm=true if the user confirmed."}
        return await run("send_sms", {"number": number, "text": text}, device_id)

    @mcp.tool()
    async def call(number: str, confirm: bool = False, device_id: str = "") -> dict:
        """Place a phone call. Needs confirm=true - set it ONLY if the user
        clearly asked to call this number (unless restrictions are lifted)."""
        if not confirm and elevation.level() == "normal":
            return {"ok": False, "needs_confirmation": True,
                    "error": f"Calling {number} requires confirmation. "
                             "Call again with confirm=true if the user confirmed."}
        return await run("call", {"number": number}, device_id)

    @mcp.tool()
    async def elevation_status() -> dict:
        """Current restriction level (normal / elevated / unrestricted),
        time left, and requests waiting for approval on the phone."""
        return elevation.status()

    @mcp.tool()
    async def request_elevation(level: str, minutes: int, reason: str, ctx: Context) -> dict:
        """Ask for the command restrictions to be lifted for a while.
        level: "elevated" (commands that need confirmation stop asking) or
        "unrestricted" (commands outside the whitelist are allowed too; deny
        rules still apply). Use ONLY when the user explicitly asked to lift
        restrictions. Nothing changes until the user approves the request on
        the phone; tell them to confirm it there and quote the code."""
        device_id = origin(ctx).get("device_id", "unknown")
        try:
            challenge = elevation.request(level, minutes * 60, reason, device_id)
        except ElevationError as exc:
            return {"ok": False, "error": str(exc)}
        logger.warning("Elevation requested: level=%s ttl=%ss device=%s code=%s reason=%r",
                       challenge.level, challenge.ttl_s, device_id, challenge.code, challenge.reason)
        return {"ok": True, "pending": True, "code": challenge.code,
                "message": "Waiting for approval in the phone app (expires in 2 min)."}

    return mcp
