import asyncio
import base64
import itertools
import json
import pathlib
import re
import pytest

from resmed_air_ble import AirSenseClient, AirSenseError, AuthError
from resmed_air_ble.protocol import (
    T_DEV_ENC,
    T_DEV_PLAIN,
    T_APP_ENC,
    Reassembler,
    SessionCipher,
    build_frame,
    challenge_response,
    encode_json,
)
from resmed_air_ble.srp import SrpClient, SrpError, SrpServer, client_proof, server_proof

CAPTURE = pathlib.Path(__file__).parents[1] / "research" / "api_samples" / "figlog_pair2.txt"


def test_srp_roundtrip():
    srv = SrpServer("1234")
    cli = SrpClient("1234")
    m1 = cli.process_challenge(srv.B.hex(), srv.salt.hex())
    m2 = srv.process_client(cli.public_key_hex, m1)
    assert cli.verify_server(m2) == srv.key
    assert len(srv.key) == 32


def test_srp_wrong_passkey():
    srv = SrpServer("1234")
    cli = SrpClient("4321")
    m1 = cli.process_challenge(srv.B.hex(), srv.salt.hex())
    with pytest.raises(SrpError):
        srv.process_client(cli.public_key_hex, m1)


def test_srp_rejects_zero_b():
    with pytest.raises(SrpError):
        SrpClient("1234").process_challenge("00", "00" * 32)


def _capture_messages():
    """Reassembles the last pairing from the modded-app log."""
    lines = CAPTURE.read_text(encoding="utf-8").splitlines()
    start = max(i for i, l in enumerate(lines) if '"method":"GetPairKey"' in l)  # last pairing in the log
    reasm = {"TX": Reassembler(), "RX": Reassembler()}
    frames, logged = [], []
    for l in lines[start : start + 20]:
        m = re.match(r"(TX|RX)FRAME (\S+)", l)
        if m:
            frames += [(m.group(1), t, p) for t, p in reasm[m.group(1)].feed(base64.b64decode(m.group(2)))]
        elif l.startswith("RX {"):
            logged.append(json.loads(l[3:]))
    return frames, logged


@pytest.mark.skipif(not CAPTURE.exists(), reason="real capture is local-only (gitignored)")
def test_against_real_pairing_capture():
    frames, logged = _capture_messages()
    plain = [json.loads(p) for _, t, p in frames if not t & 4]
    ske = next(m for m in plain if m.get("method") == "StartKeyExchange")["params"]
    res1 = next(m for m in plain if "serverPk" in m.get("result", {}))["result"]
    m1 = next(m for m in plain if m.get("method") == "ConfirmKeyExchange")["params"]["clientConfirmation"]
    res2 = next(m for m in plain if "serverConfirmation" in m.get("result", {}))["result"]
    creds = next(m for m in logged if "masterPairKey" in m.get("result", {}))["result"]
    A, B = bytes.fromhex(ske["clientPk"]), bytes.fromhex(res1["serverPk"])
    salt, key = bytes.fromhex(res1["salt"]), bytes.fromhex(creds["masterPairKey"])
    assert client_proof(A, B, salt, key).hex().upper() == m1
    assert server_proof(A, bytes.fromhex(m1), key).hex().upper() == res2["serverConfirmation"]
    cipher = SessionCipher(key, res2["nonce"])
    assert cipher.key.hex().upper() == creds["sessionKey"]
    assert res2["clientId"] == creds["clientId"]
    # first encrypted device frame after pairing decrypts with that session key
    enc = next(p for d, t, p in frames if d == "RX" and t == T_DEV_ENC)
    assert cipher.decrypt(enc)["result"]["subscriptionId"]


class FakeDevice:
    """Minimal AirSense emulator speaking FIG over a fake BleakClient interface."""

    mtu_size = 185

    def __init__(self, passkey="1234"):
        self.passkey = passkey
        self.cb = None
        self.rx = Reassembler()
        self.srp = None
        self.client_id = "A1B2C3D4E5F6"
        self.mpk = None
        self.cipher = None
        self.challenge = "11" * 32

    async def start_notify(self, _char, cb):
        self.cb = cb

    async def stop_notify(self, _char):
        self.cb = None

    def _send(self, obj, enc=False):
        if enc:
            frame = build_frame(self.cipher.encrypt(obj), T_DEV_ENC)
        else:
            frame = build_frame(encode_json(obj), T_DEV_PLAIN)
        for i in range(0, len(frame), 60):  # device fragments notifications
            self.cb(None, bytearray(frame[i : i + 60]))

    async def write_gatt_char(self, _char, data, response=True):
        assert len(data) <= self.mtu_size - 3
        for typ, payload in self.rx.feed(data):
            msg = self.cipher.decrypt(payload) if typ == T_APP_ENC else json.loads(payload)
            self._handle(msg)

    def _handle(self, m):
        rid, meth, p = m["id"], m["method"], m.get("params")
        if meth == "StartKeyExchange":
            self.srp = SrpServer(self.passkey)
            self.client_pk = p["clientPk"]
            self._send({"id": rid, "result": {"serverPk": self.srp.B.hex().upper(), "salt": self.srp.salt.hex()}})
        elif meth == "ConfirmKeyExchange":
            try:
                m2 = self.srp.process_client(self.client_pk, p["clientConfirmation"])
            except SrpError:
                self._send({"id": rid, "error": {"code": -11005, "message": "VerificationFailure"}})
                return
            self.mpk = self.srp.key
            nonce = "22" * 32
            self.cipher = SessionCipher(self.mpk, nonce)
            self._send({"id": rid, "result": {"clientId": self.client_id, "serverConfirmation": m2, "nonce": nonce}})
        elif meth == "RequestSession":
            if p["clientId"] != self.client_id:
                self._send({"id": rid, "error": {"code": -11005, "message": "VerificationFailure"}})
                return
            self._nonce = "33" * 32
            self._send({"id": rid, "result": {"challenge": self.challenge, "nonce": self._nonce}})
        elif meth == "CheckSessionIntegrity":
            ok = p["response"] == challenge_response(self.mpk, self.challenge)
            if ok:
                self.cipher = SessionCipher(self.mpk, self._nonce)
            self._send({"id": rid, "result": {"confirmation": ok}})
        elif meth == "SubscribeEvent":
            self._send({"id": rid, "result": {"subscriptionId": 5}}, enc=True)
            self._send({"method": "EventNotification", "params": {"dataId": "FGState", "subscriptionId": 5,
                        "events": [{"event": "ValueChange", "value": "Therapy"}]}}, enc=True)
        elif meth == "Get":
            self._send({"id": rid, "result": {n: "x" for n in p}}, enc=True)


def test_client_pair_reconnect_subscribe():
    async def run():
        dev = FakeDevice()
        events = []
        c = AirSenseClient(dev, on_event=lambda d, v, e: events.append((d, v)))
        await c.start()
        creds = await c.pair("1234")
        assert creds.client_id == "A1B2C3D4E5F6" and creds.master_pair_key == dev.mpk
        await c.subscribe()
        assert events == [("FGState", "Therapy")]
        # reconnect with stored creds
        c2 = AirSenseClient(dev, on_event=lambda d, v, e: events.append((d, v)))
        await c2.start()
        await c2.open_session(creds)
        assert (await c2.get("SerialNumber")) == {"SerialNumber": "x"}

    asyncio.run(run())


def test_client_wrong_passkey():
    async def run():
        dev = FakeDevice()
        c = AirSenseClient(dev)
        await c.start()
        with pytest.raises(AuthError):
            await c.pair("0000")

    asyncio.run(run())


def test_summary_decoder():
    from pbwrite import summary_day
    from resmed_air_ble.summary import last_night, parse_summary_spool

    day = 1_790_161_200_000
    blob = summary_day(day, 313, [(day + 51_480_000, 19), (day + 52_680_000, 19), (day + 53_880_000, 275)])
    blob += summary_day(day + 86_400_000, 0, [])
    days = parse_summary_spool(blob)
    assert len(days) == 2
    n = last_night(days)
    assert n is days[0]
    assert n.usage_minutes == 313 and n.usage_hours == 5.22 and n.mask_on_count == 3
    assert (n.ahi, n.ai, n.hi, n.oai, n.cai) == (0.9, 0.7, 0.1, 0.3, 0.3)  # hundredths, like STR.edf
    assert n.leak_lps == {"median": 0.2, "p70": 0.24, "p95": 0.44, "max": 0.68}
    assert n.target_pressure == {"median": 10.2, "p95": 11.4, "max": 12.6}
    assert n.target_epap["median"] == 7.2 and n.mask_pressure["median"] == 8.0
    assert n.tidal_volume_l["median"] == 0.48 and n.minute_vent["median"] == 7.25
    assert n.resp_rate["median"] == 14.4
    assert n.blower_pressure == {"p5": 4.74, "p95": 10.9} and n.flow_lps == {"p5": 0.16, "p95": 0.5}
    assert (n.ambient_humidity, n.humidifier_temp, n.humidifier_power, n.blower_flow_lps) == (15.6, 23.2, 7.6, 0.64)
    assert days[1].ahi is None


def test_client_spool():
    async def run():
        dev = FakeDevice()
        c = AirSenseClient(dev)
        await c.start()
        creds = await c.pair("1234")
        payload = bytes(range(256)) * 20
        chunks = [payload[i : i + 2000] for i in range(0, len(payload), 2000)]

        def handle(m, orig=dev._handle):
            if m["method"] == "StartSpool":
                dev._send({"id": m["id"], "result": {"spoolId": 7}}, enc=True)
            elif m["method"] == "PullSpoolFragments":
                dev._send({"id": m["id"], "result": {"spoolId": 7}}, enc=True)
                for seq, ch in enumerate(chunks):
                    last = seq == len(chunks) - 1
                    dev._send({"method": "SpoolFragment", "params": {
                        "spoolId": 7, "seq": seq, "data": base64.b64encode(ch).decode(),
                        "status": "SPOOL_COMPLETE_NO_MORE_DATA" if last else "SPOOL_INCOMPLETE"}}, enc=True)
            else:
                orig(m)

        dev._handle = handle
        import datetime as dt
        assert await c.spool("Summary", dt.datetime.now(dt.UTC)) == payload

    asyncio.run(run())

def test_client_spool_read_unavailable_and_cap():
    """spool_read keeps status/fragments; DATA_UNAVAILABLE and byte cap stay local."""
    from resmed_air_ble.client import SpoolRead

    async def run():
        dev = FakeDevice()
        c = AirSenseClient(dev)
        await c.start()
        await c.pair("1234")
        mode = {"name": "ok"}

        def handle(m, orig=dev._handle):
            if m["method"] == "StartSpool":
                dev._send({"id": m["id"], "result": {"spoolId": 9}}, enc=True)
            elif m["method"] == "PullSpoolFragments":
                dev._send({"id": m["id"], "result": {"spoolId": 9}}, enc=True)
                if mode["name"] == "unavailable":
                    dev._send(
                        {
                            "method": "SpoolFragment",
                            "params": {
                                "spoolId": 9,
                                "seq": 0,
                                "data": "",
                                "status": "ERROR_DATA_UNAVAILABLE",
                            },
                        },
                        enc=True,
                    )
                else:
                    blob = b"\x0a\x04\x08\x01\x10\x02" + b"Z" * 40
                    dev._send(
                        {
                            "method": "SpoolFragment",
                            "params": {
                                "spoolId": 9,
                                "seq": 0,
                                "data": base64.b64encode(blob).decode(),
                                "status": "SPOOL_COMPLETE_NO_MORE_DATA",
                                "spoolHash": "abc",
                            },
                        },
                        enc=True,
                    )
            else:
                orig(m)

        dev._handle = handle
        import datetime as dt

        mode["name"] = "unavailable"
        miss = await c.spool_read("SurveyEvents", dt.datetime.now(dt.UTC))
        assert isinstance(miss, SpoolRead)
        assert miss.status == "ERROR_DATA_UNAVAILABLE"
        assert miss.data == b""
        assert miss.fragments == 1

        mode["name"] = "ok"
        capped = await c.spool_read("Summary", dt.datetime.now(dt.UTC), max_bytes=10)
        assert capped.truncated is True
        assert len(capped.data) == 10
        assert capped.fragments == 1

    asyncio.run(run())


def test_client_start_spool_params_match_apk():
    """StartSpool must use APK kl0 maxSpoolSize=4096 and single-name spoolAddress.

    Request shape matches the APK: kl0.DEFAULT_MAX_SPOOL_SIZE = 4096 and GsonUtil.java:78-79.
    Live AS11 refuses 5 of 15 spool names with JSON-RPC -32602 while 10 work under the same shape.
    The cause is not established; see captures/spool-refusal-matrix-2026-10-16.json in resmed-apk-re.
    """

    async def run():
        dev = FakeDevice()
        c = AirSenseClient(dev)
        await c.start()
        await c.pair("1234")
        seen: list[dict] = []

        def handle(m, orig=dev._handle):
            if m["method"] == "StartSpool":
                seen.append(m["params"])
                # Fake refuses any maxSpoolSize above the APK value (4096) to pin the request shape.
                if m["params"].get("maxSpoolSize", 0) > 4096:
                    dev._send(
                        {
                            "id": m["id"],
                            "error": {"code": -32602, "message": "Invalid Params"},
                        },
                        enc=True,
                    )
                    return
                dev._send({"id": m["id"], "result": {"spoolId": 11}}, enc=True)
            elif m["method"] == "PullSpoolFragments":
                dev._send({"id": m["id"], "result": {"spoolId": 11}}, enc=True)
                dev._send(
                    {
                        "method": "SpoolFragment",
                        "params": {
                            "spoolId": 11,
                            "seq": 0,
                            "data": base64.b64encode(b"\x08\x01").decode(),
                            "status": "SPOOL_COMPLETE_NO_MORE_DATA",
                            "spoolHash": "dead",
                        },
                    },
                    enc=True,
                )
            else:
                orig(m)

        dev._handle = handle
        import datetime as dt

        since = dt.datetime(2026, 9, 16, 18, 19, 38, tzinfo=dt.UTC)
        read = await c.spool_read("TherapyEvents", since)
        assert read.status == "SPOOL_COMPLETE_NO_MORE_DATA"
        assert read.data == b"\x08\x01"
        assert len(seen) == 1
        params = seen[0]
        assert params["maxSpoolSize"] == 4096
        assert list(params["spoolAddress"]) == ["TherapyEvents"]
        assert params["spoolAddress"]["TherapyEvents"]["fromDateTime"] == "2026-09-16T18:19:38.000Z"

    asyncio.run(run())




def test_reassembler_recovers_after_lost_notification():
    f1 = build_frame(b"x" * 300, T_DEV_PLAIN)
    f2 = build_frame(encode_json({"ok": 1}), T_DEV_PLAIN)
    r = Reassembler()
    out = r.feed(f1[:100])  # middle notification of f1 lost
    out += r.feed(f1[200:])
    out += r.feed(f2)
    assert out == [(T_DEV_PLAIN, encode_json({"ok": 1}))]


def test_client_spool_missing_fragment_fails_fast():
    async def run():
        dev = FakeDevice()
        c = AirSenseClient(dev)
        await c.start()
        await c.pair("1234")

        def handle(m, orig=dev._handle):
            if m["method"] in ("StartSpool", "PullSpoolFragments"):
                dev._send({"id": m["id"], "result": {"spoolId": 3}}, enc=True)
                if m["method"] == "PullSpoolFragments":
                    for seq in (0, 2):  # seq 1 lost
                        dev._send({"method": "SpoolFragment", "params": {"spoolId": 3, "seq": seq, "data": "",
                                   "status": "SPOOL_COMPLETE_NO_MORE_DATA" if seq == 2 else "SPOOL_INCOMPLETE"}}, enc=True)
            else:
                orig(m)

        dev._handle = handle
        import datetime as dt
        with pytest.raises(AirSenseError, match="missing"):
            await c.spool("Summary", dt.datetime.now(dt.UTC))

    asyncio.run(run())


def test_reassembler_magic_inside_payload():
    from resmed_air_ble.protocol import MAGIC

    payload = b"ab" + MAGIC + b"\x00" * 40 + MAGIC + b"cd"
    frame = build_frame(payload, T_DEV_PLAIN)
    r = Reassembler()
    out = []
    for i in range(0, len(frame), 7):  # tiny notifications
        out += r.feed(frame[i : i + 7])
    assert out == [(T_DEV_PLAIN, payload)]


SDCARD = pathlib.Path(__file__).parents[1] / "research" / "sdcard" / "STR.edf"
SPOOL = pathlib.Path(__file__).parents[1] / "research" / "api_samples" / "spool120.jsonl"


@pytest.mark.skipif(not (SDCARD.exists() and SPOOL.exists()), reason="SD card copy is local-only (gitignored)")
def test_summary_matches_sdcard_str_edf():
    """Every decoded Summary field equals the machine's own STR.edf value for the same night."""
    import datetime as dt

    from edf import read_edf
    from resmed_air_ble.summary import parse_summary_spool

    frags = [json.loads(l) for l in SPOOL.read_text().splitlines()]
    frags = sorted((r["params"] for r in frags if r["kind"] == "spool" and r["spool"] == "Summary"), key=lambda p: p["seq"])
    days = {d.day_start.strftime("%Y-%m-%d"): d for d in parse_summary_spool(b"".join(base64.b64decode(p["data"]) for p in frags))}
    start, _, _, recs = read_edf(str(SDCARD))
    flat = {"ahi": "AHI", "ai": "AI", "hi": "HI", "oai": "OAI", "cai": "CAI", "uai": "UAI", "rin": "RIN",
            "mask_events": "MaskEvents", "usage_minutes": "Duration"}
    nested = {"leak_lps": "Leak", "target_pressure": "TgtIPAP", "mask_pressure": "MaskPress", "tidal_volume_l": "TidVol",
              "minute_vent": "MinVent", "resp_rate": "RespRate"}
    pct = {"median": "50", "p70": "70", "p95": "95", "max": "Max"}
    checked = 0
    for i, rec in enumerate(recs):
        d = days.get((start + dt.timedelta(days=i)).strftime("%Y-%m-%d"))
        if not d or d.usage_minutes < 5:
            continue
        for attr, sig in flat.items():
            assert abs(getattr(d, attr) - rec[sig]) < 0.011, (d.day_start, attr)
        for attr, sig in nested.items():
            for k, v in getattr(d, attr).items():
                assert abs(v - rec[f"{sig}.{pct[k]}"]) < 0.011, (d.day_start, attr, k)
        checked += 1
    assert checked > 60


def test_cloud_upload_and_soundcheck_logs():
    from pbwrite import cloud_session, soundcheck_run
    from resmed_air_ble.logs import parse_cloud_uploads, parse_soundchecks

    t = 1_790_213_857_000
    ups = parse_cloud_uploads(cloud_session(t) + cloud_session(t + 3_600_000, (200, 500)) + cloud_session(t + 7_200_000, end=False))
    assert [u.ok for u in ups] == [True, False, False]  # 5xx fails; unfinished session is not ok
    assert ups[0].http_statuses == [200, 202, 202, 202] and ups[0].items_sent == 3
    assert (ups[0].end - ups[0].start).total_seconds() == 25
    runs = parse_soundchecks(soundcheck_run(t) + soundcheck_run(t + 7 * 86_400_000))
    assert len(runs) == 2 and (runs[1] - runs[0]).days == 7


def test_open_session_timeout_is_not_an_auth_error():
    """Regression: a silent device (proxy hiccup) must not be reported as 'credentials rejected'."""
    async def run():
        dev = FakeDevice()
        c = AirSenseClient(dev)
        await c.start()
        creds = await c.pair("1234")
        orig = dev._handle
        dev._handle = lambda m: None if m["method"] == "RequestSession" else orig(m)  # no answer at all
        c2 = AirSenseClient(dev)
        c2.timeout = 0.2
        await c2.start()
        with pytest.raises(AirSenseError) as exc:
            await c2.open_session(creds)
        assert not isinstance(exc.value, AuthError)
        dev._handle = orig  # an unknown client is explicitly rejected (-11005) -> AuthError
        c3 = AirSenseClient(dev)
        await c3.start()
        with pytest.raises(AuthError):
            await c3.open_session(type(creds)("FFFFFFFFFFFF", creds.master_pair_key))

    asyncio.run(run())


def test_normalize_fg_state_apk_codes():
    """Constructed fixture: APK libfiglib nameMappings 0..9 (PROTOCOL.md §5 / fng + ZRM)."""
    from resmed_air_ble import FG_STATE_BY_CODE, FG_THERAPY_STATES, normalize_fg_state

    assert FG_STATE_BY_CODE[1] == "Standby" and FG_STATE_BY_CODE[3] == "Therapy"
    assert FG_STATE_BY_CODE[4] == "MaskFit" and FG_STATE_BY_CODE[5] == "TestMode"
    assert normalize_fg_state(0) == "Reset"
    assert normalize_fg_state(3) == "Therapy"
    assert normalize_fg_state("3") == "Therapy"
    assert normalize_fg_state(3.0) == "Therapy"
    assert normalize_fg_state("Standby") == "Standby"
    assert normalize_fg_state("  MaskFit ") == "MaskFit"
    assert normalize_fg_state(None) is None
    assert "Therapy" in FG_THERAPY_STATES and "TestMode" in FG_THERAPY_STATES
    assert "Standby" not in FG_THERAPY_STATES


def test_client_spool_resumes_on_more_data_pending():
    """Constructed fixture: SPOOL_COMPLETE_MORE_DATA_PENDING + nextSpoolAddress (APK ccg/i36)."""
    async def run():
        dev = FakeDevice()
        c = AirSenseClient(dev)
        await c.start()
        await c.pair("1234")
        part1 = b"AAAA" * 50
        part2 = b"BBBB" * 50
        starts: list[str] = []
        spool_ids = itertools.count(10)

        def handle(m, orig=dev._handle):
            if m["method"] == "StartSpool":
                sid = next(spool_ids)
                addr = m["params"]["spoolAddress"]["Summary"]["fromDateTime"]
                starts.append(addr)
                dev._send({"id": m["id"], "result": {"spoolId": sid}}, enc=True)
            elif m["method"] == "PullSpoolFragments":
                sid = m["params"]["spoolId"]
                dev._send({"id": m["id"], "result": {"spoolId": sid}}, enc=True)
                if sid == 10:
                    # first page complete, more pending at a later fromDateTime
                    dev._send({"method": "SpoolFragment", "params": {
                        "spoolId": sid, "seq": 0, "data": base64.b64encode(part1).decode(),
                        "status": "SPOOL_COMPLETE_MORE_DATA_PENDING",
                        "spoolHash": "hash-page-1",
                        "nextSpoolAddress": {
                            "Summary": {"fromDateTime": "2026-09-24T12:00:00.000Z"},
                        },
                    }}, enc=True)
                else:
                    dev._send({"method": "SpoolFragment", "params": {
                        "spoolId": sid, "seq": 0, "data": base64.b64encode(part2).decode(),
                        "status": "SPOOL_COMPLETE_NO_MORE_DATA",
                        "spoolHash": "hash-page-2",
                    }}, enc=True)
            else:
                orig(m)

        dev._handle = handle
        import datetime as dt
        since = dt.datetime(2026, 9, 10, tzinfo=dt.UTC)
        assert await c.spool("Summary", since) == part1 + part2
        assert len(starts) == 2
        assert starts[0].startswith("2026-09-10")
        assert starts[1] == "2026-09-24T12:00:00.000Z"

    asyncio.run(run())


def test_client_spool_more_data_pending_without_next_stops():
    """Constructed fixture: MORE_DATA_PENDING with null fromDateTime stops (APK i36 log path)."""
    async def run():
        dev = FakeDevice()
        c = AirSenseClient(dev)
        await c.start()
        await c.pair("1234")
        payload = b"only-page"

        def handle(m, orig=dev._handle):
            if m["method"] == "StartSpool":
                dev._send({"id": m["id"], "result": {"spoolId": 1}}, enc=True)
            elif m["method"] == "PullSpoolFragments":
                dev._send({"id": m["id"], "result": {"spoolId": 1}}, enc=True)
                dev._send({"method": "SpoolFragment", "params": {
                    "spoolId": 1, "seq": 0, "data": base64.b64encode(payload).decode(),
                    "status": "SPOOL_COMPLETE_MORE_DATA_PENDING",
                    "spoolHash": "h1",
                    "nextSpoolAddress": {"Summary": {"fromDateTime": None}},
                }}, enc=True)
            else:
                orig(m)

        dev._handle = handle
        import datetime as dt
        assert await c.spool("Summary", dt.datetime.now(dt.UTC)) == payload

    asyncio.run(run())


def test_summary_statistics_rows_multi_night():
    """Constructed fixture: multi-day Summary → external statistic rows for graphs."""
    import sys
    from pathlib import Path

    # summary_statistics lives in the HA component package
    root = Path(__file__).parents[1]
    sys.path.insert(0, str(root))
    from pbwrite import summary_day
    from custom_components.resmed_airsense.summary_statistics import build_summary_statistic_rows
    from resmed_air_ble.summary import parse_summary_spool

    day = 1_790_161_200_000
    blob = summary_day(day, 313, [(day + 51_480_000, 313)])
    blob += summary_day(day + 86_400_000, 0, [])  # unused night skipped
    blob += summary_day(day + 2 * 86_400_000, 400, [(day + 2 * 86_400_000 + 50_000_000, 400)])
    days = parse_summary_spool(blob)
    jobs = build_summary_statistic_rows("12345678901", days)
    by_id = {meta["statistic_id"]: (meta, rows) for meta, rows in jobs}
    assert "resmed_airsense:12345678901_usage" in by_id
    assert "resmed_airsense:12345678901_ahi" in by_id
    assert "resmed_airsense:12345678901_leak_p95" in by_id
    assert "resmed_airsense:12345678901_pressure_p95" in by_id
    usage_meta, usage_rows = by_id["resmed_airsense:12345678901_usage"]
    assert usage_meta["source"] == "resmed_airsense" and usage_meta["has_mean"] is True
    assert len(usage_rows) == 2  # only nights with usage
    assert usage_rows[0]["mean"] == 5.22
    assert usage_rows[1]["mean"] == round(400 / 60, 2)
    ahi_rows = by_id["resmed_airsense:12345678901_ahi"][1]
    assert ahi_rows[0]["mean"] == 0.9
    leak_rows = by_id["resmed_airsense:12345678901_leak_p95"][1]
    assert leak_rows[0]["mean"] == 26.4  # 0.44 L/s * 60
    pressure_rows = by_id["resmed_airsense:12345678901_pressure_p95"][1]
    assert pressure_rows[0]["mean"] == 11.4
    # hour-aligned UTC starts, distinct per night
    starts = [r["start"] for r in usage_rows]
    assert all(s.minute == 0 and s.second == 0 for s in starts)
    assert starts[0] != starts[1]
