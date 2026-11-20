import pytest

from homescreen import focus
from homescreen.model import Layout, LayoutError

from fake_phone import app, folder, widget

WEBCLIP = {"displayIdentifier": "CLIP1", "displayName": "Clip", "webClipURL": "https://example.com"}


def stat(days, opens, minutes):
    return {"days": days, "opens": opens, "seconds": minutes * 60}


def layout(library=()):
    """Dock of 2, a page with a medium widget, a folder and loose apps, a second page, a web clip."""
    a = {i: app(f"com.x.a{i:02d}", f"A{i:02d}") for i in range(40)}
    state = [
        [a[0], a[1]],
        [widget("Weather", "medium"), folder("Stuff", [a[2], a[3], a[4]]), a[5], a[6]],
        [a[i] for i in range(7, 30)] + [WEBCLIP],
    ]
    return Layout(state, library=[a[i] for i in range(30, 40)] + list(library))


def names(page):
    return [n.name for n in page]


def test_score_prefers_daily_use_over_one_long_session():
    assert focus.score(stat(28, 60, 30)) > focus.score(stat(2, 6, 120))


def test_one_page_keeps_dock_and_widget_and_sends_the_rest_to_the_library():
    lay = layout()
    stats = {f"com.x.a{i:02d}": stat(30 - i, 100 - i, 50) for i in range(2, 40)}
    stats["com.x.a00"] = stat(30, 500, 500)  # in the dock: stays there, not on page 1
    out = focus.focus(lay, {b: s["opens"] for b, s in stats.items()}, stats, pages=1)
    assert names(lay.dock) == ["A00", "A01"]
    page = lay.pages[0]
    assert len(lay.pages) == 1 and page[0].kind == "widget" and page[-1].name == "Clip"
    # 24 slots - 8 for the widget - 1 for the web clip = 15 apps, best first
    assert names(page[1:-1]) == [f"A{i:02d}" for i in range(2, 17)]
    assert out["sent_to_library"] == 13 and lay.validate() == []
    assert {n.name for n in lay.library} == {f"A{i:02d}" for i in range(17, 40)}


def test_unused_apps_leave_and_used_library_apps_come_back():
    lay = layout()
    stats = {"com.x.a05": stat(20, 80, 40), "com.x.a35": stat(25, 90, 60)}  # a35 is App Library only
    out = focus.focus(lay, {b: s["opens"] for b, s in stats.items()}, stats, pages=1)
    assert names(lay.pages[0]) == ["Weather", "A35", "A05", "Clip"]
    assert out["from_library"] == 1 and out["sent_to_library"] == 27
    assert not any(n.kind == "folder" for n in lay.pages[0]) and lay.validate() == []


def test_two_pages_file_the_next_tier_by_category():
    lay = layout()
    stats = {f"com.x.a{i:02d}": stat(30 - i // 2, 100 - i, 50) for i in range(2, 30)}
    cats = {f"com.x.a{i:02d}": ("Finance" if i % 2 else "Travel") for i in range(2, 28)}
    cats["com.x.a28"] = "Weather"  # the only one in its category, and a29 has none: both go to Other
    out = focus.focus(lay, {b: s["opens"] for b, s in stats.items()}, stats, cats, pages=2)
    assert names(lay.pages[0]) == ["Weather"] + [f"A{i:02d}" for i in range(2, 18)]
    second = lay.pages[1]
    assert names(second) == ["Travel", "Finance", "Other", "Clip"]
    assert out["folders"]["Other"] == ["A28", "A29"]
    assert [len(p) for p in second[0].children] == [5] and lay.validate() == []


def test_two_pages_without_categories_are_loose():
    lay = layout()
    stats = {f"com.x.a{i:02d}": stat(30, 100 - i, 50) for i in range(2, 30)}
    out = focus.focus(lay, {b: s["opens"] for b, s in stats.items()}, stats, pages=2)
    assert names(lay.pages[1]) == [f"A{i:02d}" for i in range(18, 30)] + ["Clip"]
    assert out["folders"] == {} and lay.validate() == []


def test_too_many_categories_fold_into_other():
    a = [app(f"com.y.b{i:02d}", f"B{i:02d}") for i in range(80)]
    lay = Layout([[], a[:24], a[24:48], a[48:72]], library=a[72:])
    stats = {x["bundleIdentifier"]: stat(30, 200 - i, 10) for i, x in enumerate(a)}
    cats = {x["bundleIdentifier"]: f"Cat {i % 28:02d}" for i, x in enumerate(a[24:])}  # 28 categories of 2
    focus.focus(lay, {b: s["opens"] for b, s in stats.items()}, stats, cats, pages=2)
    second = lay.pages[1]
    assert len(second) == 24 and second[-1].name == "Other" and lay.validate() == []
    assert sum(len(p) for p in second[-1].children) == 56 - 23 * 2


def test_needs_usage_and_an_app_library():
    with pytest.raises(LayoutError, match="no usage"):
        focus.focus(layout(), {}, None)
    state = layout().to_state()
    with pytest.raises(LayoutError, match="App Library"):
        focus.focus(Layout(state), {"com.x.a05": 3}, None)


def test_falls_back_to_opens_without_stats():
    lay = layout()
    focus.focus(lay, {"com.x.a09": 5, "com.x.a12": 9}, None, pages=1)
    assert names(lay.pages[0]) == ["Weather", "A12", "A09", "Clip"]


def test_second_copy_of_an_app_is_kept_and_the_ordinary_icon_is_ranked():
    twin = {"iconType": "app", "bundleIdentifier": "com.x.a05", "displayIdentifier": "UUID-5", "iconLists": []}
    a = [app(f"com.x.a{i:02d}", f"A{i:02d}") for i in range(8)]
    lay = Layout([[a[0]], [twin, a[1], a[2]], [a[5], a[6]]], library=[a[7]])
    focus.focus(lay, {"com.x.a05": 9, "com.x.a01": 4}, None, pages=1)
    first, second, kept = lay.pages[0]
    assert first.name == "A05" and second.name == "A01" and kept.raw is twin and lay.validate() == []
