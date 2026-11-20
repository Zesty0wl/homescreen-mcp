"""Screen Time usage from this Mac, and page-swipe estimates.

With Screen Time's "Share Across Devices" on, macOS keeps the iPhone's app-focus history in
Biome: ~/Library/Biome/streams/restricted/App.InFocus/remote/<device>/<file>. Files are SEGB
logs named by their start time (microseconds since 2001-01-01); each record holds the bundle id
of the app in focus. Reading them needs Full Disk Access for the app running this code.

Each record is a protobuf message: field 3 says whether the app came into focus or left it,
field 4 is the time (seconds since 2001-01-01) and field 6 the bundle id. Decoding them gives,
per app, how often it was opened, for how long, and on how many days. If a file isn't in the
SEGB v2 layout we know, we fall back to counting bundle-id occurrences and halving, which
still ranks apps well enough for the optimizer.
"""

from __future__ import annotations

import re
import struct
import time
import zlib
from collections import Counter
from pathlib import Path
from typing import Any

from .model import Layout, Node

BIOME = Path.home() / "Library/Biome/streams/restricted/App.InFocus/remote"
MAC_EPOCH = 978307200  # 2001-01-01 in Unix time
BUNDLE_RE = re.compile(rb"[A-Za-z0-9][A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_\-]+){1,}")


def _file_time(p: Path) -> float | None:
    try:
        return int(p.name) / 1e6 + MAC_EPOCH
    except ValueError:
        return None


# ---- SEGB v2 records ------------------------------------------------------------------------
# File: 32-byte header ("SEGB", uint32 record count, ...), the records, then one 16-byte trailer
# entry per record at the end of the file, last record first: int32 end offset (from the end of
# the header), uint32 state, float64 creation time. A record is crc32 + 4 bytes + protobuf, and
# records start on 4-byte boundaries.

SEGB_HEADER = 32
SEGB_TRAILER = struct.Struct("<iId")
SEGB_WRITTEN = 1
MAX_SESSION = 4 * 3600  # a longer gap means the "left focus" record is missing


def _segb_records(data: bytes) -> list[bytes] | None:
    """Protobuf payloads of the live records, or None if this isn't a SEGB v2 file."""
    if data[:4] != b"SEGB" or len(data) < SEGB_HEADER:
        return None
    (count,) = struct.unpack_from("<I", data, 4)
    trailer = len(data) - count * SEGB_TRAILER.size
    if trailer < SEGB_HEADER:
        return None
    out, start = [], 0
    for i in range(count):
        end, state, _created = SEGB_TRAILER.unpack_from(data, len(data) - (i + 1) * SEGB_TRAILER.size)
        if end <= start or SEGB_HEADER + end > trailer:
            break
        rec = data[SEGB_HEADER + start:SEGB_HEADER + end]
        start = (end + 3) & ~3
        if state == SEGB_WRITTEN and len(rec) > 8 and zlib.crc32(rec[8:]) == struct.unpack_from("<I", rec)[0]:
            out.append(rec[8:])
    return out


def _focus_event(buf: bytes) -> tuple[float, str, bool] | None:
    """(unix time, bundle id, came into focus) from one App.InFocus record."""
    i, n = 0, len(buf)
    when = bundle = None
    starting = False
    try:
        while i < n:
            key = shift = 0
            while True:
                b = buf[i]; i += 1
                key |= (b & 0x7F) << shift
                if not b & 0x80:
                    break
                shift += 7
            field, wire = key >> 3, key & 7
            if wire == 0:
                value = shift = 0
                while True:
                    b = buf[i]; i += 1
                    value |= (b & 0x7F) << shift
                    if not b & 0x80:
                        break
                    shift += 7
                if field == 3:
                    starting = bool(value)
            elif wire == 1:
                if field == 4:
                    when = struct.unpack_from("<d", buf, i)[0] + MAC_EPOCH
                i += 8
            elif wire == 2:
                length = shift = 0
                while True:
                    b = buf[i]; i += 1
                    length |= (b & 0x7F) << shift
                    if not b & 0x80:
                        break
                    shift += 7
                if field == 6:
                    bundle = buf[i:i + length].decode("utf-8")
                i += length
            elif wire == 5:
                i += 4
            else:
                return None
    except (IndexError, struct.error, UnicodeDecodeError):
        return None
    return (when, bundle, starting) if when is not None and bundle else None


def _events(files: list[Path]) -> list[tuple[float, str, bool]] | None:
    """Every focus event in these files in time order, or None if any file can't be decoded.
    One app leaving focus and the next coming into it often share a timestamp, so events with
    the same time keep the order they were written in."""
    out: list[tuple[float, str, bool]] = []
    for f in sorted(files, key=lambda f: _file_time(f) or 0):
        records = _segb_records(f.read_bytes())
        if records is None:
            return None
        out += [e for e in map(_focus_event, records) if e]
    out.sort(key=lambda e: e[0])
    return out


def _stats(events: list[tuple[float, str, bool]], known: set[str], since: float) -> dict[str, dict[str, Any]]:
    """Per app in `known`: opens, seconds in focus and the days it was opened on. A session runs
    from an app's "came into focus" record to the next record of any app."""
    stats: dict[str, dict[str, Any]] = {}
    for (when, bundle, starting), nxt in zip(events, events[1:] + [None]):
        if not starting or when < since or bundle not in known:
            continue
        s = stats.setdefault(bundle, {"opens": 0, "seconds": 0.0, "days": set()})
        s["opens"] += 1
        s["days"].add(time.strftime("%Y-%m-%d", time.localtime(when)))
        if nxt and 0 < nxt[0] - when <= MAX_SESSION:
            s["seconds"] += nxt[0] - when
    return stats


def _counts(files: list[Path], known: set[str]) -> Counter:
    c: Counter = Counter()
    for f in files:
        for m in BUNDLE_RE.findall(f.read_bytes()):
            s = m.decode("ascii", "ignore")
            # a protobuf field tag can follow the string with no separator; trim one char if needed
            if s in known:
                c[s] += 1
            elif s[:-1] in known:
                c[s[:-1]] += 1
    return c


def load(known: set[str], days: int = 30, root: Path = BIOME) -> dict[str, Any]:
    """App opens per bundle id over the last `days`, from the synced device(s) whose apps best
    match `known` (the apps on the phone being arranged). When the records decode, "stats" also
    has each app's seconds in focus and the number of days it was opened on."""
    if not root.exists():
        return {"ok": False, "error": "No synced Screen Time data on this Mac. Turn on Screen Time > "
                "Share Across Devices on both the iPhone and the Mac (same Apple Account)."}
    since = time.time() - days * 86400
    devices = []
    try:
        dirs = [d for d in root.iterdir() if d.is_dir()]
    except PermissionError:
        return {"ok": False, "error": "macOS blocked access to Screen Time data. Give your terminal (or the "
                "app running Homescreen) Full Disk Access in System Settings > Privacy & Security."}
    for d in dirs:
        every = [f for f in d.iterdir() if f.is_file()]
        files = [f for f in every if (_file_time(f) or 0) >= since]
        # a file that starts before the window can still end inside it; decoded events carry
        # their own time, so read every file and let _stats drop the old ones
        events = _events(every) if every else None
        if events is not None:
            stats = _stats(events, known, since)
            if stats:
                devices.append({"id": d.name, "apps": len(stats), "stats": stats,
                                "events": sum(s["opens"] for s in stats.values()) * 2,
                                "last": max(e[0] for e in events)})
            continue
        if not files:
            continue
        c = _counts(files, known)
        if c:
            devices.append({"id": d.name, "apps": len(c), "events": sum(c.values()), "counts": c,
                            "last": max(_file_time(f) or 0 for f in files)})
    if not devices:
        return {"ok": False, "error": f"No Screen Time data from this iPhone in the last {days} days."}
    # one phone can appear under several device ids (e.g. after an iOS update); merge every
    # device that looks like the same phone: at least half the apps of the best match
    devices.sort(key=lambda d: d["apps"], reverse=True)
    best = devices[0]["apps"]
    used = [d for d in devices if d["apps"] >= best / 2]
    total: Counter = Counter()
    merged: dict[str, dict[str, Any]] = {}
    for d in used:
        if "stats" not in d:
            total.update({b: max(1, round(n / 2)) for b, n in d["counts"].items()})
            continue
        for b, s in d["stats"].items():
            m = merged.setdefault(b, {"opens": 0, "seconds": 0.0, "days": set()})
            m["opens"] += s["opens"]
            m["seconds"] += s["seconds"]
            m["days"] |= s["days"]
            total[b] += s["opens"]
    out: dict[str, Any] = {"ok": True, "days": days, "opens": dict(total),
                           "devices": [{k: d[k] for k in ("id", "apps", "events", "last")} for d in used]}
    if merged:
        out["stats"] = {b: {"opens": m["opens"], "seconds": round(m["seconds"]), "days": len(m["days"])}
                        for b, m in merged.items()}
    return out


# ---- usage from the device itself ----------------------------------------------------------
# Without Screen Time synced to this Mac, the device's JetsamEvent reports still show which apps
# were held in memory, several times a day. An app in memory on many days is one the person
# opens, with two catches: iOS also wakes apps in the background (notifications, calls, widgets,
# location), and keeps some resident all the time. Those are discounted, not trusted.

BACKGROUND_MODES = {"voip"}  # calls wake an app and keep it in memory without it being opened


def from_memory_reports(files: list[Path], apps: dict[str, dict[str, Any]], known: set[str]) -> dict[str, Any]:
    """apps: installed apps as installation_proxy returns them (executable name, background modes)."""
    import json as _json
    exe = {a.get("CFBundleExecutable"): b for b, a in apps.items() if a.get("CFBundleExecutable")}
    days: dict[str, set[str]] = {}
    seen: Counter = Counter()  # reports an app is in
    front: Counter = Counter()
    report_days: set[str] = set()
    for f in files:
        try:
            body = _json.loads(f.read_text(errors="ignore").split("\n", 1)[1])
        except (ValueError, IndexError):
            continue
        day = str(body.get("date", ""))[:10] or f.name
        report_days.add(day)
        for p in body.get("processes", []):
            b = exe.get(p.get("name"))
            states = p.get("states") or []
            if not b or b not in known or "daemon" in states:
                continue
            days.setdefault(b, set()).add(day)
            seen[b] += 1
            if "frontmost" in states:
                front[b] += 1
    if not report_days:
        return {"ok": False, "error": "This device has no memory reports yet (iOS writes them now and then). "
                "Try again after a few days of use, or use Screen Time."}
    n = len(report_days)
    opens: dict[str, int] = {}
    background: list[str] = []
    for b, d in days.items():
        score = len(d) * 10 + seen[b] + front[b] * 5
        modes = set(apps.get(b, {}).get("UIBackgroundModes") or [])
        if n >= 7 and len(d) >= 0.9 * n:          # in memory on (nearly) every day of a week+: resident
            score //= 4
            background.append(b)
        elif modes & BACKGROUND_MODES:            # may have been woken, not opened
            score //= 2
            background.append(b)
        opens[b] = max(1, score)
    return {"ok": True, "source": "device", "days": n, "reports": len(files), "opens": opens,
            "background": sorted(background)}


# ---- swipes -----------------------------------------------------------------------------

def _app_positions(lay: Layout) -> dict[str, int]:
    """Swipes to reach each app from page 1: page N costs N-1, a folder's page K adds K-1,
    the dock is free, an App Library-only app costs one swipe past the last page."""
    cost: dict[str, int] = {}
    for n in lay.dock:
        for leaf in _leaves(n):
            cost[leaf.bundle_id or leaf.id] = 0
    for i, page in enumerate(lay.pages):
        for n in page:
            if n.kind == "folder":
                for k, fp in enumerate(n.children):
                    for c in fp:
                        cost[c.bundle_id or c.id] = i + k
            elif n.kind in ("app", "webclip"):
                cost[n.bundle_id or n.id] = i
    for n in lay.library or []:
        cost[n.bundle_id or n.id] = len(lay.pages)
    return cost


def _leaves(n: Node) -> list[Node]:
    return [c for p in n.children for c in p] if n.kind == "folder" else [n]


def swipes(lay: Layout, opens: dict[str, int]) -> int:
    cost = _app_positions(lay)
    return sum(opens.get(b, 0) * c for b, c in cost.items())


def top_apps(lay: Layout, opens: dict[str, int], n: int = 25,
             stats: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The most-opened apps and where they are. With `stats` (see load), each also has the
    minutes it was in focus and the number of days it was opened on."""
    names = {x.bundle_id: x.name for x in lay.nodes.values() if x.bundle_id}
    pos = lay.positions()
    ranked = sorted(((b, c) for b, c in opens.items() if b in names), key=lambda kv: -kv[1])[:n]
    out = [{"id": b, "name": names[b], "opens": c, "where": pos.get(b, "")} for b, c in ranked]
    for row in out:
        s = (stats or {}).get(row["id"])
        if s:
            row["minutes"] = round(s["seconds"] / 60)
            row["days_used"] = s["days"]
    return out


# ---- optimizer --------------------------------------------------------------------------

def optimize(lay: Layout, opens: dict[str, int], keep_first_page: bool = False) -> dict[str, Any]:
    """Put the most-opened apps and folders on the earliest pages (row by row) and the most
    opened apps on each folder's first page. Widgets stay on their pages; the dock and the
    App Library aren't touched. Returns the swipe estimate before and after."""
    before = swipes(lay, opens)
    score = lambda n: sum(opens.get(c.bundle_id or c.id, 0) for c in _leaves(n))
    for n in list(lay.nodes.values()):  # folder contents first, so folder scores stay the same
        if n.kind == "folder":
            items = sorted(_leaves(n), key=lambda c: -opens.get(c.bundle_id or c.id, 0))
            lay.refold(n, items)
    start = 1 if keep_first_page and lay.pages else 0
    lay.unpin_pages(start)
    pages = lay.pages[start:]
    widgets = [[x for x in p if x.kind == "widget"] for p in pages]
    movable = sorted((x for p in pages for x in p if x.kind != "widget"), key=lambda n: -score(n))
    out: list[list[Node]] = []
    i = 0
    for k in range(max(len(pages), 1)):
        page = list(widgets[k]) if k < len(widgets) else []
        used = sum(x.slots for x in page)
        while i < len(movable) and used + movable[i].slots <= lay.page_slots:
            page.append(movable[i])
            used += movable[i].slots
            i += 1
        out.append(page)
    while i < len(movable):  # anything left gets new pages
        page, used = [], 0
        while i < len(movable) and used + movable[i].slots <= lay.page_slots:
            page.append(movable[i])
            used += movable[i].slots
            i += 1
        out.append(page)
    lay.pages = lay.pages[:start] + [p for p in out if p]
    return {"swipes_before": before, "swipes_after": swipes(lay, opens)}
