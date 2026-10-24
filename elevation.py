"""
elevation.py

Temporarily lifting the command restrictions - and the rule that only the
phone can do it.

Levels:
    normal        the whitelist as written: "confirm" commands need
                  confirm=true, unmatched commands are refused
    elevated      "confirm" commands run without asking
    unrestricted  unmatched commands run too; the whitelist is bypassed

On every level the deny rules stay in force (destructive patterns, the
audit log), and so does the phone's own app deny-list (banks, 2FA).

A request can come from anywhere - a voice command makes Claude call the
phone MCP tool request_elevation - but it only becomes a *challenge*. It is
granted when someone taps "confirm" in the app, i.e. through the mTLS
gateway with the phone's client certificate. Lifting straight from the app
(/elevation/request) is granted at once for the same reason: the request
itself already comes from the phone.

The state lives here, in the orchestrator. The executor asks for the
current level (read-only) before classifying each command, so a revoke or
an expiry applies to the very next command.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass

LEVELS = ("normal", "elevated", "unrestricted")

DEFAULT_TTL_S = int(os.environ.get("ELEVATION_DEFAULT_TTL_S", 600))
MAX_TTL_S = int(os.environ.get("ELEVATION_MAX_TTL_S", 3600))
CHALLENGE_TTL_S = 120
# Optional second factor: if set, confirming a voice-raised request also
# needs this PIN typed on the phone.
ELEVATION_PIN = os.environ.get("ELEVATION_PIN", "")


class ElevationError(ValueError):
    """Rejected request; the message is shown in the app."""


@dataclass
class Challenge:
    id: str
    code: str
    level: str
    ttl_s: int
    reason: str
    device_id: str
    created_at: float

    def expires_in(self, now: float) -> int:
        return max(0, int(self.created_at + CHALLENGE_TTL_S - now))

    def to_json(self, now: float) -> dict:
        return {
            "id": self.id,
            "code": self.code,
            "level": self.level,
            "ttl_s": self.ttl_s,
            "reason": self.reason,
            "device_id": self.device_id,
            "expires_in_s": self.expires_in(now),
        }


def _check(level: str, ttl_s: int) -> int:
    if level not in LEVELS or level == "normal":
        raise ElevationError(f"Unknown level '{level}' (elevated or unrestricted)")
    if ttl_s <= 0:
        ttl_s = DEFAULT_TTL_S
    return min(ttl_s, MAX_TTL_S)


class Elevation:
    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._level = "normal"
        self._until = 0.0
        self._granted_by = ""
        self._reason = ""
        self._pending: dict[str, Challenge] = {}

    # -- internal -------------------------------------------------------

    def _expire(self, now: float) -> None:
        if self._level != "normal" and now >= self._until:
            self._level, self._until, self._granted_by, self._reason = "normal", 0.0, "", ""
        for cid in [c.id for c in self._pending.values() if c.expires_in(now) == 0]:
            del self._pending[cid]

    def _grant(self, level: str, ttl_s: int, granted_by: str, reason: str, now: float) -> None:
        self._level, self._until = level, now + ttl_s
        self._granted_by, self._reason = granted_by, reason

    # -- reads ----------------------------------------------------------

    def level(self) -> str:
        with self._lock:
            self._expire(self._clock())
            return self._level

    def status(self) -> dict:
        with self._lock:
            now = self._clock()
            self._expire(now)
            return {
                "level": self._level,
                "remaining_s": max(0, int(self._until - now)) if self._level != "normal" else 0,
                "granted_by": self._granted_by,
                "reason": self._reason,
                "pending": [c.to_json(now) for c in self._pending.values()],
                "passphrase_mode": False,
                "pin_required": bool(ELEVATION_PIN),
                "default_ttl_s": DEFAULT_TTL_S,
            }

    # -- changes --------------------------------------------------------

    def request(self, level: str, ttl_s: int, reason: str, device_id: str) -> Challenge:
        """A voice command asks for elevation: create a challenge for the phone."""
        ttl_s = _check(level, ttl_s)
        with self._lock:
            now = self._clock()
            self._expire(now)
            challenge = Challenge(
                id=secrets.token_hex(8),
                code=f"{secrets.randbelow(10_000):04d}",
                level=level,
                ttl_s=ttl_s,
                reason=reason.strip()[:200],
                device_id=device_id,
                created_at=now,
            )
            self._pending[challenge.id] = challenge
            return challenge

    def confirm(self, challenge_id: str, pin: str | None, confirmed_by: str) -> Challenge:
        with self._lock:
            now = self._clock()
            self._expire(now)
            challenge = self._pending.get(challenge_id)
            if challenge is None:
                raise ElevationError("Request not found or expired")
            if ELEVATION_PIN and not hmac.compare_digest((pin or "").encode(), ELEVATION_PIN.encode()):
                raise ElevationError("Wrong PIN")
            del self._pending[challenge_id]
            self._grant(challenge.level, challenge.ttl_s, confirmed_by, challenge.reason, now)
            return challenge

    def deny(self, challenge_id: str) -> None:
        with self._lock:
            self._pending.pop(challenge_id, None)

    def lift(self, level: str, ttl_s: int, reason: str, device_id: str) -> tuple[str, int]:
        """Lifted straight from the app - the phone is the approver."""
        ttl_s = _check(level, ttl_s)
        with self._lock:
            now = self._clock()
            self._expire(now)
            self._grant(level, ttl_s, device_id, reason.strip()[:200], now)
            return level, ttl_s

    def revoke(self) -> None:
        with self._lock:
            self._level, self._until, self._granted_by, self._reason = "normal", 0.0, "", ""
            self._pending.clear()
