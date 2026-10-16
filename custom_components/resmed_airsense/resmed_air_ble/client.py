"""Async client for ResMed Air-family devices over an already-connected BleakClient.

Transport-agnostic: works with a plain `bleak.BleakClient` (CLI / dev) or with the client
returned by `bleak_retry_connector.establish_connection` inside Home Assistant.
Read-only by design: only pairing, session, subscribe and Get are exposed.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import itertools
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .protocol import (
    ENCRYPTED_BIT,
    RX_CHAR,
    T_APP_ENC,
    T_APP_PLAIN,
    TX_CHAR,
    Credentials,
    Reassembler,
    SessionCipher,
    build_frame,
    challenge_response,
    encode_json,
)
from .srp import SrpClient

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
DEFAULT_EVENTS = ("TestDriveState", "FGState", "RecoverableError", "SystemError")


class AirSenseError(Exception):
    """Protocol-level failure (error reply, timeout, bad handshake)."""


class RpcError(AirSenseError):
    """The device answered with a JSON-RPC error (as opposed to not answering at all)."""

    def __init__(self, method: str, error: dict) -> None:
        super().__init__(f"{method}: {error}")
        self.code = error.get("code") if isinstance(error, dict) else None
        self.message = error.get("message") if isinstance(error, dict) else None


class AuthError(AirSenseError):
    """Credentials rejected / wrong passKey — re-pairing needed.

    Only raised when the device explicitly says so; timeouts and lost frames stay AirSenseError (retry).
    """


VERIFICATION_FAILURE = -11005  # device reply when the clientId/key is not (or no longer) paired


EventCallback = Callable[[str, Any, dict], None]  # (dataId, value, raw_event)
NotificationCallback = Callable[[str, dict], None]  # (method, params) for SpoolFragment, StreamData, ...

@dataclass(frozen=True)
class SpoolRead:
    """Assembled spool payload plus the terminal fragment metadata the APK exposes."""

    data: bytes
    status: str
    fragments: int
    spool_hash: str | None = None
    truncated: bool = False



class AirSenseClient:
    def __init__(
        self,
        ble_client: Any,
        on_event: EventCallback | None = None,
        on_notification: NotificationCallback | None = None,
    ) -> None:
        self._ble = ble_client
        self._on_event = on_event
        self._on_notification = on_notification
        self._reasm = Reassembler()
        self._cipher: SessionCipher | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._spools: dict[int, asyncio.Queue] = {}
        self._ids = itertools.count(2)
        self._write_size = 20
        self.timeout = DEFAULT_TIMEOUT  # per request

    @property
    def session_open(self) -> bool:
        return self._cipher is not None

    async def start(self) -> None:
        backend = getattr(self._ble, "_backend", None)
        if backend is not None and hasattr(backend, "_acquire_mtu"):
            try:  # BlueZ starts at MTU 23 and would truncate long writes
                await backend._acquire_mtu()
            except Exception:  # noqa: BLE001 - best effort
                pass
        self._write_size = max(20, (getattr(self._ble, "mtu_size", 23) or 23) - 3)
        await self._ble.start_notify(RX_CHAR, self._on_notify)

    async def stop(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        try:
            await self._ble.stop_notify(RX_CHAR)
        except Exception:  # noqa: BLE001 - already disconnected
            pass

    # ---- handshakes -------------------------------------------------------------------

    async def pair(self, passkey: str) -> Credentials:
        """First-time pairing with the code shown on the device screen (SRP-6a).

        Leaves an encrypted session open. The device assigns a new clientId each time;
        any previous clientId for this client becomes invalid.
        """
        srp = SrpClient(passkey)
        r = await self._call("StartKeyExchange", {"clientPk": srp.public_key_hex}, "2.0", req_id=1)
        m1 = srp.process_challenge(r["serverPk"], r["salt"])
        try:
            r = await self._call("ConfirmKeyExchange", {"clientConfirmation": m1}, "2.0", req_id=1)
        except RpcError as err:  # the device answered "no": wrong code
            raise AuthError(f"device rejected passKey: {err}") from err
        mpk = srp.verify_server(r["serverConfirmation"])
        self._cipher = SessionCipher(mpk, r["nonce"])
        return Credentials(r["clientId"], mpk)

    async def open_session(self, creds: Credentials) -> None:
        """Reconnect with stored credentials (no screen code)."""
        try:
            r = await self._call("RequestSession", {"clientId": creds.client_id}, "2.0", req_id=1)
        except RpcError as err:
            if err.code == VERIFICATION_FAILURE:
                raise AuthError(f"RequestSession rejected: {err}") from err
            raise
        cipher = SessionCipher(creds.master_pair_key, r["nonce"])
        resp = challenge_response(creds.master_pair_key, r["challenge"])
        r = await self._call("CheckSessionIntegrity", {"response": resp}, "2.0", req_id=1)
        if not r.get("confirmation"):
            raise AuthError("CheckSessionIntegrity: confirmation false")
        self._cipher = cipher

    # ---- session requests (encrypted) ---------------------------------------------------

    async def subscribe(self, data_ids: tuple[str, ...] = DEFAULT_EVENTS) -> dict:
        return await self._call("SubscribeEvent", {"dataIds": list(data_ids)}, "1.0", encrypted=True)

    async def get(self, *names: str) -> dict:
        return await self._call("Get", list(names), "1.0", encrypted=True)

    async def spool(self, address: str, since: dt.datetime, idle_timeout: float = 5.0) -> bytes:
        """Reads a logged-data spool (e.g. "Summary") from `since`; returns the concatenated payload.

        Fragments arrive back to back. A lost one (BLE proxies drop notifications under load) shows up
        as a gap in `seq` or as silence and raises AirSenseError so the caller can retry. When the
        device answers `SPOOL_COMPLETE_MORE_DATA_PENDING` (APK `ccg$c` / `i36`), this re-issues
        `StartSpool` from `nextSpoolAddress.<Name>.fromDateTime` and keeps appending until
        `SPOOL_COMPLETE_NO_MORE_DATA` (or an empty next address).
        """
        return (await self.spool_read(address, since, idle_timeout=idle_timeout)).data

    async def spool_read(
        self,
        address: str,
        since: dt.datetime,
        idle_timeout: float = 5.0,
        max_bytes: int | None = None,
    ) -> SpoolRead:
        """Like `spool`, but keeps status / fragment count / hash and optionally caps payload size."""
        from_dt = since.astimezone(dt.UTC)
        assembled = bytearray()
        seen_hashes: set[str] = set()
        fragments = 0
        status = ""
        spool_hash: str | None = None
        truncated = False
        # Bound the resume chain so a buggy device cannot loop forever.
        for _round in range(32):
            payload, status, next_from, spool_hash, frag_count = await self._spool_once(
                address, from_dt, idle_timeout
            )
            fragments += frag_count
            if spool_hash:
                if spool_hash in seen_hashes:
                    _LOGGER.debug("spool %s: duplicate spoolHash %s; stopping resume", address, spool_hash)
                    break
                seen_hashes.add(spool_hash)
            if max_bytes is not None and len(assembled) + len(payload) > max_bytes:
                remain = max(0, max_bytes - len(assembled))
                assembled.extend(payload[:remain])
                truncated = True
                break
            assembled.extend(payload)
            if status == "ERROR_DATA_UNAVAILABLE":
                break
            if status != "SPOOL_COMPLETE_MORE_DATA_PENDING":
                break
            if next_from is None:
                _LOGGER.debug(
                    "spool %s: MORE_DATA_PENDING without nextSpoolAddress.fromDateTime; stopping",
                    address,
                )
                break
            from_dt = next_from
        return SpoolRead(
            data=bytes(assembled),
            status=status or "UNKNOWN",
            fragments=fragments,
            spool_hash=spool_hash,
            truncated=truncated,
        )

    async def _spool_once(
        self, address: str, since: dt.datetime, idle_timeout: float
    ) -> tuple[bytes, str, dt.datetime | None, str | None, int]:
        """One StartSpool + PullSpoolFragments cycle. Returns (payload, status, next_from, hash, frags)."""
        # Request shape matches the APK: kl0.DEFAULT_MAX_SPOOL_SIZE = 4096 and GsonUtil.java:78-79
        # (Joda UTC toString). On live AS11, 5 of 15 spool names (TherapyEvents, UsageEvents,
        # SystemActivityEvents, SystemExceptionEvents, DiagnosticExceptionEvents) are refused with
        # JSON-RPC -32602 Invalid Params; the other 10 succeed with this same shape. The cause is
        # not established; see captures/spool-refusal-matrix-2026-10-16.json in resmed-apk-re.
        params = {
            "maxSpoolSize": 4096,
            "spoolAddress": {
                address: {"fromDateTime": since.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
            },
        }
        spool_id = (await self.call("StartSpool", params))["spoolId"]
        queue: asyncio.Queue = asyncio.Queue()
        self._spools[spool_id] = queue
        try:
            await self.call(
                "PullSpoolFragments",
                {"maxFragmentSize": 2808, "maxNotifications": 0, "spoolId": spool_id},
            )
            chunks: dict[int, bytes] = {}
            last_frag: dict[str, Any] = {}
            while True:
                try:
                    frag = await asyncio.wait_for(queue.get(), idle_timeout)
                except TimeoutError as err:
                    raise AirSenseError(f"spool {address}: stalled after {len(chunks)} fragment(s)") from err
                seq = frag.get("seq", 0)
                chunks[seq] = base64.b64decode(frag.get("data") or "")
                status = frag.get("status", "")
                last_frag = frag
                _LOGGER.debug("spool %s #%s seq %s: %s", address, spool_id, seq, status)
                if status == "ERROR_DATA_UNAVAILABLE":
                    return b"", status, None, frag.get("spoolHash"), len(chunks)
                if status.startswith("SPOOL_COMPLETE"):
                    if sorted(chunks) != list(range(seq + 1)):
                        raise AirSenseError(f"spool {address}: missing fragment(s), got {sorted(chunks)}")
                    payload = b"".join(chunks[k] for k in range(seq + 1))
                    return (
                        payload,
                        status,
                        self._next_spool_from(address, last_frag),
                        last_frag.get("spoolHash"),
                        len(chunks),
                    )
        finally:
            self._spools.pop(spool_id, None)


    @staticmethod
    def _next_spool_from(address: str, frag: dict[str, Any]) -> dt.datetime | None:
        """Parse nextSpoolAddress.<Name>.fromDateTime from a complete spool fragment (APK ccg)."""
        nxt = frag.get("nextSpoolAddress")
        if not isinstance(nxt, dict):
            return None
        entry = nxt.get(address) or next(iter(nxt.values()), None)
        if not isinstance(entry, dict):
            return None
        raw = entry.get("fromDateTime")
        if not raw:
            return None
        try:
            # Machine timestamps are ISO-8601; tolerate a trailing Z.
            text = str(raw).replace("Z", "+00:00")
            return dt.datetime.fromisoformat(text).astimezone(dt.UTC)
        except ValueError:
            return None

    # ---- plumbing ----------------------------------------------------------------------

    async def call(self, method: str, params: Any = None, version: str = "1.0", timeout: float | None = None) -> Any:
        """Generic encrypted session request (read-only methods only, by convention)."""
        return await self._call(method, params, version, encrypted=True, timeout=timeout)

    async def _call(
        self,
        method: str,
        params: Any,
        version: str,
        *,
        req_id: int | None = None,
        encrypted: bool = False,
        timeout: float | None = None,
    ) -> Any:
        rid = req_id if req_id is not None else next(self._ids)
        msg = {"id": rid, "jsonrpc": version, "method": method}
        if params is not None:
            msg["params"] = params
        if encrypted:
            if self._cipher is None:
                raise AirSenseError("no session")
            frame = build_frame(self._cipher.encrypt(msg), T_APP_ENC)
        else:
            frame = build_frame(encode_json(msg), T_APP_PLAIN)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            for i in range(0, len(frame), self._write_size):
                await self._ble.write_gatt_char(TX_CHAR, frame[i : i + self._write_size], response=True)
            reply = await asyncio.wait_for(fut, timeout or self.timeout)
        except asyncio.TimeoutError as err:
            raise AirSenseError(f"{method}: timeout") from err
        finally:
            self._pending.pop(rid, None)
        if "error" in reply:
            raise RpcError(method, reply["error"])
        return reply.get("result")

    def _on_notify(self, _char: Any, data: bytearray) -> None:
        for typ, payload in self._reasm.feed(bytes(data)):
            if typ & ENCRYPTED_BIT:
                msg = self._cipher.decrypt(payload) if self._cipher else None
            else:
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    msg = None
            if isinstance(msg, dict):
                self._dispatch(msg)
            else:
                _LOGGER.debug("undecodable frame type %#06x (%d bytes)", typ, len(payload))

    def _dispatch(self, msg: dict) -> None:
        if "method" in msg and "id" not in msg:
            p = msg.get("params", {})
            if msg["method"] == "EventNotification" and self._on_event:
                for ev in p.get("events", []):
                    self._on_event(p.get("dataId"), ev.get("value"), ev)
            elif msg["method"] == "SpoolFragment" and p.get("spoolId") in self._spools:
                self._spools[p["spoolId"]].put_nowait(p)
            elif msg["method"] != "HeartBeat" and self._on_notification:
                self._on_notification(msg["method"], p)
            return
        if "method" in msg:  # echo of our own request
            return
        fut = self._pending.get(msg.get("id"))
        if fut and not fut.done():
            fut.set_result(msg)
