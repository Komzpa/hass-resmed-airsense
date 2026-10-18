"""FIG framing, session crypto and constants for ResMed Air-family devices (no I/O)."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import zlib
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

SERVICE_UUID = "0000fd56-0000-1000-8000-00805f9b34fb"
TX_CHAR = "a6220002-35f1-4b20-afae-cb089d2044aa"  # write, app -> device
RX_CHAR = "a6220003-35f1-4b20-afae-cb089d2044aa"  # notify, device -> app
NAME_PREFIX = "ResMed "

MAGIC = bytes.fromhex("bebafeca")
HEADER_LEN = 16
T_DEV_PLAIN = 0x0392  # device -> app, clear (heartbeat, handshake results)
T_APP_PLAIN = 0x0393  # app -> device, clear (handshake requests)
T_DEV_ENC = 0x0396  # device -> app, encrypted session
T_APP_ENC = 0x0397  # app -> device, encrypted session
ENCRYPTED_BIT = 0x0004

_LOGGER = logging.getLogger(__name__)

# FGState values from libfiglib nameMappings (shortName ZRM). Wire EventNotification
# usually carries the string form; native/periodic paths may carry the int code.
FG_STATE_BY_CODE: dict[int, str] = {
    0: "Reset",
    1: "Standby",
    2: "ResetCompliance",
    3: "Therapy",
    4: "MaskFit",
    5: "TestMode",
    6: "SystemError",
    7: "Upgrade",
    8: "UpgradePreparation",
    9: "Setup",
}
# All native ZRM names above, plus valid wire strings outside that numeric table.
# TestDrive is used by FgState.kt; Running and Backup are observed firmware replies.
# Backup has no evidenced numeric code: do not assign one or replace it with unknown.
FG_STATE_NAMES: tuple[str, ...] = (
    *FG_STATE_BY_CODE.values(),
    "TestDrive",
    "Running",
    "Backup",
)

FG_THERAPY_STATES = frozenset({"Therapy", "MaskFit", "TestDrive", "Running", "TestMode"})


def normalize_fg_state(value: object) -> str | None:
    """Map an FGState EventNotification/Get value to its APK string name."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return FG_STATE_BY_CODE.get(value, str(value))
    if isinstance(value, float) and value.is_integer():
        return FG_STATE_BY_CODE.get(int(value), str(int(value)))
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return FG_STATE_BY_CODE.get(int(text), text)
    return text


@dataclass(frozen=True)
class Credentials:
    """Result of pairing; enough to reconnect without the screen code."""

    client_id: str
    master_pair_key: bytes

    def as_dict(self) -> dict[str, str]:
        return {"client_id": self.client_id, "master_pair_key": self.master_pair_key.hex().upper()}

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> Credentials:
        return cls(
            d.get("client_id") or d["clientId"],
            bytes.fromhex(d.get("master_pair_key") or d["masterPairKey"]),
        )


def _crc(b: bytes) -> bytes:
    return zlib.crc32(b).to_bytes(4, "little")


def build_frame(payload: bytes, typ: int) -> bytes:
    mid = typ.to_bytes(2, "little") + len(payload).to_bytes(2, "little") + _crc(payload)
    return MAGIC + mid + _crc(mid) + payload


def encode_json(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


class Reassembler:
    """Joins BLE notifications into complete FIG frames; yields (type, payload)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buf += data
        out: list[tuple[int, bytes]] = []
        while True:
            i = self._buf.find(MAGIC)
            if i < 0:
                del self._buf[:-3]
                break
            del self._buf[:i]
            if len(self._buf) < HEADER_LEN:
                break
            hdr = bytes(self._buf[4:12])
            if _crc(hdr) != bytes(self._buf[12:16]):
                _LOGGER.debug("header CRC mismatch, resyncing")
                del self._buf[:1]  # false magic, resync
                continue
            ln = int.from_bytes(hdr[2:4], "little")
            if len(self._buf) < HEADER_LEN + ln:
                # still waiting: if a new valid header shows up inside, a notification of this frame was lost
                nxt = self._next_header(1)
                if nxt < 0:
                    break
                _LOGGER.debug("dropped incomplete frame type %#06x len %d: next frame started", int.from_bytes(hdr[0:2], "little"), ln)
                del self._buf[:nxt]
                continue
            payload = bytes(self._buf[HEADER_LEN : HEADER_LEN + ln])
            if _crc(payload) == hdr[4:8]:
                del self._buf[: HEADER_LEN + ln]
                out.append((int.from_bytes(hdr[0:2], "little"), payload))
            else:
                # a notification was lost: the declared length ran into the next frame, so only
                # skip this header and resync on the next magic instead of eating the next frame
                _LOGGER.debug("dropped frame type %#06x len %d: payload CRC mismatch", int.from_bytes(hdr[0:2], "little"), ln)
                del self._buf[:1]
        return out


    def _next_header(self, start: int) -> int:
        """Offset of the next magic followed by a CRC-valid header, or -1."""
        i = start
        while (i := self._buf.find(MAGIC, i)) >= 0 and len(self._buf) >= i + HEADER_LEN:
            if _crc(bytes(self._buf[i + 4 : i + 12])) == bytes(self._buf[i + 12 : i + 16]):
                return i
            i += 1
        return -1


class SessionCipher:
    """AES-256-CBC session: payload = IV(16) || E(len(2 LE) || json || zeropad16)."""

    def __init__(self, master_pair_key: bytes, nonce_hex: str) -> None:
        self.key = hashlib.sha256(master_pair_key + bytes.fromhex(nonce_hex)).digest()

    def encrypt(self, obj: dict) -> bytes:
        js = encode_json(obj)
        inner = len(js).to_bytes(2, "little") + js
        inner += b"\x00" * (-len(inner) % 16)
        iv = os.urandom(16)
        enc = Cipher(algorithms.AES(self.key), modes.CBC(iv)).encryptor()
        return iv + enc.update(inner) + enc.finalize()

    def decrypt(self, payload: bytes) -> dict | None:
        iv, ct = payload[:16], payload[16:]
        if not ct or len(ct) % 16:
            return None
        dec = Cipher(algorithms.AES(self.key), modes.CBC(iv)).decryptor()
        d = dec.update(ct) + dec.finalize()
        ln = int.from_bytes(d[:2], "little")
        try:
            return json.loads(d[2 : 2 + ln].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None


def challenge_response(master_pair_key: bytes, challenge_hex: str) -> str:
    return hmac.new(master_pair_key, bytes.fromhex(challenge_hex), hashlib.sha256).hexdigest().upper()
