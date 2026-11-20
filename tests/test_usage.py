import struct
import time
import zlib

from homescreen import usage

MAC_EPOCH = 978307200


def noon_yesterday() -> float:
    """A fixed time of day, so sessions a few minutes apart never straddle midnight."""
    t = time.localtime(time.time() - 86400)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 12, 0, 0, 0, 0, -1))


def varint(n: int) -> bytes:
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def record(when: float, bundle: str, starting: bool) -> bytes:
    """One App.InFocus protobuf: 3 = starting, 4 = time since 2001, 6 = bundle id."""
    b = bundle.encode()
    return (b"\x10\x01" + b"\x18" + varint(int(starting)) + b"\x21" + struct.pack("<d", when - MAC_EPOCH)
            + b"\x32" + varint(len(b)) + b + b"\x4a\x031.0")


def segb(records: list[bytes], deleted: set[int] = frozenset()) -> bytes:
    body, trailer = b"", []
    for i, payload in enumerate(records):
        body += struct.pack("<II", zlib.crc32(payload), 0) + payload
        trailer.append(struct.pack("<iId", len(body), 3 if i in deleted else 1, 0.0))
        body += b"\0" * (-len(body) % 4)
    header = b"SEGB" + struct.pack("<I", len(records)) + b"\0" * 24
    return header + body + b"\0" * 64 + b"".join(reversed(trailer))


def biome(tmp_path, device: str, data: bytes, start: float) -> None:
    d = tmp_path / device
    d.mkdir(parents=True, exist_ok=True)
    (d / "tombstone").mkdir(exist_ok=True)
    (d / str(int((start - MAC_EPOCH) * 1e6))).write_bytes(data)


def test_decodes_opens_time_and_days(tmp_path):
    now = noon_yesterday()
    day = 86400
    events = [
        (now - 3 * day, "com.a", True), (now - 3 * day + 600, "com.a", False),     # 10 min
        (now - 2 * day, "com.a", True), (now - 2 * day + 60, "com.b", True),       # 1 min, then b
        (now - 2 * day + 180, "com.b", False),                                       # b: 2 min
        (now - 1 * day, "com.a", True), (now - 1 * day + 30, "com.a", False),      # 30 s
        (now - 1 * day + 40, "com.unknown", True), (now - 1 * day + 50, "com.unknown", False),
        (now - 3600, "com.c", True),                                                 # still open: no time
    ]
    biome(tmp_path, "PHONE", segb([record(*e) for e in events]), now - 3 * day)
    u = usage.load({"com.a", "com.b", "com.c"}, days=30, root=tmp_path)
    assert u["ok"] and u["opens"] == {"com.a": 3, "com.b": 1, "com.c": 1}
    assert u["stats"]["com.a"] == {"opens": 3, "seconds": 690, "days": 3}
    assert u["stats"]["com.b"] == {"opens": 1, "seconds": 120, "days": 1}
    assert u["stats"]["com.c"]["seconds"] == 0


def test_window_uses_event_times_not_file_names(tmp_path):
    now = noon_yesterday()
    old, recent = now - 40 * 86400, now - 86400
    events = [(old, "com.a", True), (old + 60, "com.a", False),
              (recent, "com.a", True), (recent + 60, "com.a", False)]
    # the file starts before the window but its last session is inside it
    biome(tmp_path, "PHONE", segb([record(*e) for e in events]), old)
    u = usage.load({"com.a"}, days=30, root=tmp_path)
    assert u["stats"]["com.a"] == {"opens": 1, "seconds": 60, "days": 1}


def test_skips_deleted_and_corrupt_records(tmp_path):
    now = noon_yesterday()
    records = [record(now - 300, "com.a", True), record(now - 200, "com.a", False),
               record(now - 100, "com.b", True), record(now - 50, "com.b", False)]
    data = bytearray(segb(records, deleted={2}))
    data[32 + 8 + 3] ^= 0xFF  # break the first record: its checksum no longer matches
    biome(tmp_path, "PHONE", bytes(data), now - 300)
    u = usage.load({"com.a", "com.b"}, days=30, root=tmp_path)
    assert not u["ok"]  # only "left focus" records survive, so nothing was opened


def test_unknown_file_format_falls_back_to_counting(tmp_path):
    now = noon_yesterday()
    biome(tmp_path, "PHONE", b"\0" * 56 + b"SEGB" + b"..com.a..com.a..com.b..com.b..com.a..com.a", now - 60)
    u = usage.load({"com.a", "com.b"}, days=30, root=tmp_path)
    assert u["ok"] and u["opens"] == {"com.a": 2, "com.b": 1} and "stats" not in u


def test_same_phone_under_two_ids_is_merged(tmp_path):
    now = noon_yesterday()
    one = [(now - 500, "com.a", True), (now - 400, "com.a", False)]
    two = [(now - 300, "com.a", True), (now - 200, "com.a", False)]
    biome(tmp_path, "OLD-ID", segb([record(*e) for e in one]), now - 500)
    biome(tmp_path, "NEW-ID", segb([record(*e) for e in two]), now - 300)
    u = usage.load({"com.a"}, days=30, root=tmp_path)
    assert u["stats"]["com.a"] == {"opens": 2, "seconds": 200, "days": 1} and len(u["devices"]) == 2


def test_switch_at_the_same_instant_keeps_written_order(tmp_path):
    now = noon_yesterday()
    # com.z leaves and com.a arrives at the same timestamp; sorting by name would put com.a's
    # arrival first and end its session at once
    events = [(now, "com.z", True), (now + 60, "com.z", False), (now + 60, "com.a", True),
              (now + 180, "com.a", False)]
    biome(tmp_path, "PHONE", segb([record(*e) for e in events]), now)
    u = usage.load({"com.a", "com.z"}, days=30, root=tmp_path)
    assert u["stats"]["com.z"]["seconds"] == 60 and u["stats"]["com.a"]["seconds"] == 120
