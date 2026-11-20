"""Focus: one or two pages built from what you use, everything else in the App Library.

Page 1 gets the apps you use most, loose. With two pages, page 2 gets the next tier, in folders
named after their App Store category when categories are given. Apps with no use in the period
go to the App Library, where search still finds them. The dock and widgets stay.
"""

from __future__ import annotations

import math
from typing import Any

from . import organize, usage
from .model import Layout, LayoutError, Node

OTHER = "Other"


def score(stat: dict[str, Any]) -> float:
    """How much an app belongs on the Home Screen. Days used counts most: an app opened briefly
    every day outranks one used for an hour twice. Opens and minutes are on a log scale so one
    heavy app doesn't flatten the rest."""
    return stat["days"] + 10 * math.log10(1 + stat["opens"]) + 5 * math.log10(1 + stat["seconds"] / 60)


def ranking(opens: dict[str, int], stats: dict[str, dict[str, Any]] | None) -> dict[str, float]:
    """Bundle id -> score. Without per-app stats (usage from memory reports), opens are the score."""
    if stats:
        return {b: score(s) for b, s in stats.items()}
    return {b: float(n) for b, n in opens.items() if n > 0}


def focus(lay: Layout, opens: dict[str, int], stats: dict[str, dict[str, Any]] | None = None,
          categories: dict[str, str] | None = None, pages: int = 2) -> dict[str, Any]:
    """Rebuild the Home Screen as `pages` pages (1 or 2). Returns what went where."""
    if pages not in (1, 2):
        raise LayoutError("focus builds 1 or 2 pages")
    if lay.library is None:
        raise LayoutError("this device interface doesn't expose the App Library, so the other apps "
                          "have nowhere to go")
    rank = ranking(opens, stats)
    if not rank:
        raise LayoutError("no usage data to rank apps by")
    before = usage.swipes(lay, opens)

    for folder in [n for p in lay.pages for n in p if n.kind == "folder"]:
        lay.dissolve_folder(folder.id)

    in_dock = {leaf.bundle_id for n in lay.dock for leaf in usage._leaves(n)}
    widgets: list[list[Node]] = [[] for _ in range(pages)]
    keep: list[Node] = []       # no usage to judge by, and put there on purpose: web clips, second copies
    apps: list[Node] = []
    seen = set(in_dock)
    # An app can be on the Home Screen twice: its ordinary icon, plus an entry iOS marks with an
    # iconType. The ordinary icon is the one to rank; the other copy is kept as it is.
    on_pages = [n for page in lay.pages for n in page]
    plain = {n.bundle_id for n in on_pages if n.kind == "app" and not n.raw.get("iconType")}
    for k, page in enumerate(lay.pages):
        for n in page:
            if n.kind == "widget":
                widgets[min(k, pages - 1)].append(n)
            elif (n.kind == "app" and n.bundle_id not in seen
                  and not (n.raw.get("iconType") and n.bundle_id in plain)):
                seen.add(n.bundle_id)
                apps.append(n)
            else:
                keep.append(n)
    on_screen = {n.id for n in apps}
    library = list(lay.library)
    for n in library:
        if n.kind == "app" and n.bundle_id not in seen:
            seen.add(n.bundle_id)
            apps.append(n)
    used = sorted((n for n in apps if rank.get(n.bundle_id, 0) > 0), key=lambda n: -rank[n.bundle_id])
    chosen: set[str] = set()

    def room(k: int) -> int:
        reserved = sum(w.slots for w in widgets[k]) + (len(keep) if k == pages - 1 else 0)
        return lay.page_slots - reserved

    first = used[:max(room(0), 0)]
    chosen.update(n.id for n in first)
    rest = used[len(first):]

    loose: list[Node] = []
    groups: list[tuple[str, list[Node]]] = []
    if pages == 2 and rest:
        slots = max(room(1), 0)
        if categories:
            by_cat: dict[str, list[Node]] = {}
            for n in rest:
                by_cat.setdefault(categories.get(n.bundle_id) or OTHER, []).append(n)
            other = by_cat.pop(OTHER, [])
            for name in [c for c, members in by_cat.items() if len(members) == 1]:
                other += by_cat.pop(name)  # a folder of one isn't worth a folder
            total = lambda members: sum(rank[n.bundle_id] for n in members)
            groups = sorted(by_cat.items(), key=lambda kv: -total(kv[1]))
            while len(groups) + bool(other) > slots and groups:  # too many folders: fold the least used into Other
                other += groups.pop()[1]
            if other and slots:
                groups.append((OTHER, sorted(other, key=lambda n: -rank[n.bundle_id])))
            for _, members in groups:
                chosen.update(n.id for n in members)
        else:
            loose = rest[:slots]
            chosen.update(n.id for n in loose)

    sent = [n for n in apps if n.id in on_screen and n.id not in chosen]
    lay.library = [n for n in library if n.id not in chosen] + sent
    lay.pages = [widgets[0] + first] + ([widgets[1] + loose] if pages == 2 else [])
    filed: dict[str, list[str]] = {}
    for name, members in groups:
        # the members are parked in the library list so create_folder can move them from there
        lay.library += members
        fid = lay.create_folder(name, [n.id for n in members], "page:2")
        lay.refold(lay.get(fid))
        filed[name] = [n.name for n in members]
    lay.pages[-1] += keep
    lay.pages = [p for p in lay.pages if p] or [[]]
    lay.pins = {}
    organize.check(lay)
    return {"page_1": [n.name for n in first], "page_2": [n.name for n in loose], "folders": filed,
            "kept": [n.name for n in keep], "sent_to_library": len(sent),
            "from_library": sum(1 for n in apps if n.id in chosen and n.id not in on_screen),
            "swipes_before": before, "swipes_after": usage.swipes(lay, opens)}
