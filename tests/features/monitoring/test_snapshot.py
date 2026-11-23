"""snapshot and diff."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from seoprobe.core.models import Severity
from seoprobe.core.page import Page
from seoprobe.core.rules.catalog import get
from seoprobe.features.monitoring import Snapshot, diff, snapshot
from seoprobe.features.monitoring.snapshot import change_rules, content_hash
from tests import fakes

URL = "https://example.com/p"
HTML = """<html><head>
<title>Blue widgets</title>
<meta name="description" content="All about blue widgets.">
<meta name="robots" content="max-snippet:50">
<link rel="canonical" href="/p">
<link rel="alternate" hreflang="tr" href="https://example.com/tr/p">
<link rel="alternate" hreflang="en" href="https://example.com/p">
<script type="application/ld+json">{"@type": "Product", "name": "w"}</script>
</head><body>
<h1>Blue widgets</h1>
<p>Blue widgets are   the BEST widgets.</p>
<a href="/a">a</a> <a href="https://example.com/b">b</a> <a href="https://other.test/">x</a>
</body></html>"""


async def _snap(html: str = HTML, **kw: object) -> Snapshot:
    async with fakes.session({URL: fakes.html_page(html, **kw)}) as s:  # type: ignore[arg-type]
        res = await snapshot(URL, session=s)
    assert res.ok and res.data is not None
    return res.data


async def test_snapshot_captures_the_seo_state() -> None:
    snap = await _snap()
    assert snap.status == 200 and snap.final_url == URL and snap.error is None
    assert snap.title == "Blue widgets" and snap.meta_description == "All about blue widgets."
    assert snap.canonical == URL and snap.robots_meta == ["max-snippet:50"] and not snap.noindex
    assert snap.h1 == ["Blue widgets"]
    assert [(p.lang, p.href) for p in snap.hreflang] == [("en", URL), ("tr", "https://example.com/tr/p")]
    assert snap.schema_types == ["Product"]
    assert snap.internal_links == 2 and snap.word_count > 0
    assert len(snap.content_hash) == 64 and snap.taken_at is not None


async def test_noindex_from_the_header_and_the_meta_tag() -> None:
    header = await _snap(headers={"X-Robots-Tag": "noindex"})
    assert header.noindex and header.x_robots_tag == "noindex"
    meta = await _snap(HTML.replace("max-snippet:50", "noindex"))
    assert meta.noindex and meta.robots_meta == ["noindex"]


def test_content_hash_ignores_case_and_whitespace() -> None:
    assert content_hash("Hello   World\n") == content_hash("hello world")
    assert content_hash("hello world") == hashlib.sha256(b"hello world").hexdigest()


async def test_the_same_page_gives_the_same_hash_and_an_empty_diff() -> None:
    a, b = await _snap(), await _snap()
    res = await diff(a, b)
    assert res.ok and res.data is not None and not res.data.changed and res.data.changes == []
    assert res.data.max_severity is None and res.findings == []


async def test_a_snapshot_of_a_given_page_needs_no_network() -> None:
    page = Page(url=URL, final_url=URL, status=200, html=HTML, content_type="text/html", fetched_at=datetime.now(UTC))
    async with fakes.session({}) as s:
        res = await snapshot(page, session=s)
        assert s.http.requested == []  # type: ignore[attr-defined]
    assert res.data is not None and res.data.title == "Blue widgets"


async def test_an_error_page_is_snapshotted_without_parsed_fields() -> None:
    snap = await _snap("<html><head><title>Nope</title></head></html>", status=404)
    assert snap.status == 404 and snap.title == "" and snap.content_hash == ""


async def test_an_unreachable_page_fails_the_operation() -> None:
    async with fakes.session({}, errors={URL: "unreachable"}) as s:
        res = await snapshot(URL, session=s)
    assert res.status == "error" and res.error is not None and res.error.code == "connect-failed"


async def test_diff_ranks_changes_by_severity() -> None:
    old = await _snap()
    new_html = (
        HTML.replace("max-snippet:50", "noindex")
        .replace("<title>Blue widgets", "<title>Red widgets")
        .replace('href="/p"', 'href="/other"')
        .replace("the BEST widgets.", "fine.")
    )
    new = await _snap(new_html)
    res = await diff(old, new)
    assert res.data is not None and res.data.changed
    fields = [c.field for c in res.data.changes]
    assert fields == ["noindex", "canonical", "title", "robots", "content_hash"]
    assert res.data.changes[0].severity == Severity.CRITICAL and res.data.max_severity == Severity.CRITICAL
    assert [c.severity for c in res.data.changes] == [
        Severity.CRITICAL,
        Severity.HIGH,
        Severity.MEDIUM,
        Severity.MEDIUM,
        Severity.INFO,
    ]
    ids = [f.id for f in res.findings]
    assert ids == [
        "snapshot-noindex-added",
        "snapshot-canonical-changed",
        "snapshot-title-changed",
        "snapshot-robots-changed",
        "snapshot-content-changed",
    ]
    title = next(f for f in res.findings if f.id == "snapshot-title-changed")
    assert title.evidence == "title: Blue widgets -> Red widgets"


def _s(**kw: object) -> Snapshot:
    return Snapshot.model_validate({"url": URL, "status": 200, **kw})


async def test_status_changes_and_their_direction() -> None:
    res = await diff(_s(), _s(status=503))
    assert res.data is not None and [f.id for f in res.findings] == ["snapshot-status-error"]
    res = await diff(_s(status=503), _s(status=200))
    assert [f.id for f in res.findings] == ["snapshot-status-changed"]
    res = await diff(_s(noindex=True), _s())
    assert [f.id for f in res.findings] == ["snapshot-noindex-removed"]


async def test_word_count_and_link_thresholds_are_parameters() -> None:
    old, new = _s(word_count=1000, internal_links=10), _s(word_count=650, internal_links=12)
    res = await diff(old, new)
    assert res.data is not None and [c.field for c in res.data.changes] == ["word_count"]
    assert res.data.thresholds == {"word_count_drop_pct": 30.0, "internal_links_change_pct": 30.0}
    assert "drop threshold 30%" in (res.findings[0].evidence or "")
    res = await diff(old, new, word_count_drop_pct=40, internal_links_change_pct=10)
    assert res.data is not None and [c.field for c in res.data.changes] == ["internal_links"]


async def test_diff_accepts_json_round_tripped_snapshots_without_network() -> None:
    a = await _snap()
    b = a.model_copy(update={"h1": ["Other"], "schema_types": []})
    old, new = json.loads(a.model_dump_json()), json.loads(b.model_dump_json())
    res = await diff(old, new)  # dicts, as stored by a caller
    assert res.data is not None and [c.field for c in res.data.changes] == ["h1", "schema_types"]
    assert diff.requires.network is False and res.meta.requests == []
    assert json.loads(res.to_json())["data"]["changes"][0]["old"] == ["Blue widgets"]


async def test_snapshot_result_is_json() -> None:
    async with fakes.session({URL: fakes.html_page(HTML)}) as s:
        res = await snapshot(URL, session=s)
    data = json.loads(res.to_json())["data"]
    assert Snapshot.model_validate(data).title == "Blue widgets"


def test_the_severity_data_file_agrees_with_the_catalog() -> None:
    for kind, row in change_rules().items():
        assert get(row["rule"]).severity == Severity(row["severity"]), kind
