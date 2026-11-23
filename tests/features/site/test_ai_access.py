"""Agentic module: llms.txt content, Markdown delivery, Content-Signal, traces, controls."""

from __future__ import annotations

from typing import Any

from seoprobe.core.config import Config
from seoprobe.core.net.client import MAX_PAGE_BYTES, RawResponse
from seoprobe.core.result import Result
from seoprobe.core.rules import catalog
from seoprobe.core.session import Session
from seoprobe.features.site import FEATURE, AgenticReadiness, RobotsReport, ai_access
from seoprobe.features.site.ai_access import MAX_REQUESTS, llms_txt_notes
from seoprobe.features.site.robots import parse_robots
from tests import fakes

from .support import HOST, page

AGENTIC_RULE_IDS = (
    "llms-txt-malformed",
    "markdown-not-available",
    "content-signal-missing",
    "agent-unfriendly-controls",
)


class Scripted:
    """url -> (status, headers, body). Unknown URLs answer 404. Records every request."""

    def __init__(self, table: dict[str, tuple[int, dict[str, str], str]] | None = None):
        self.table = table or {}
        self.requested: list[str] = []

    async def fetch_bytes(self, url: str, *, max_bytes: int | None = MAX_PAGE_BYTES) -> RawResponse:
        self.requested.append(url)
        status, headers, body = self.table.get(url, (404, {}, ""))
        return RawResponse(
            url, url, status, {k.lower(): v for k, v in headers.items()}, body.encode(), False, None, [], 1, 1
        )

    async def close(self) -> None:
        return None


async def _call(html: str, fetcher: Any, rb: RobotsReport | None) -> Result[AgenticReadiness]:
    async with Session(Config(), http=fetcher) as s:
        return await ai_access(page("/", html), robots=rb, session=s)


GOOD_LLMS = """# Acme

> Acme makes anvils for the discerning coyote.

## Docs

- [Guide](https://example.com/guide): how to use an anvil
- [API](https://example.com/api): the anvil API
"""
HOME = (
    '<html lang="en"><head><title>Acme</title></head><body><main><h1>Acme</h1>'
    '<button>Buy</button><label for="q">Search</label><input id="q" name="q">'
    '<a href="/x">Read more about anvils</a></main></body></html>'
)


def robots(text: str = "User-agent: *\nAllow: /\n", llms: bool | None = False) -> RobotsReport:
    return RobotsReport(status="ok", rules=parse_robots(text), llms_txt_present=llms, http_status=200)


async def run(html: str = HOME, table=None, rb=None):  # type: ignore[no-untyped-def]
    f = Scripted(table or {})
    res = await _call(html, f, rb if rb is not None else robots())
    return res, [x.id for x in res.findings], f


def status(res, cid: str) -> str:  # type: ignore[no-untyped-def]
    return next(c.status for c in res.data.checks if c.id == cid)


def test_registered_and_rules_do_not_collide() -> None:
    RULES = catalog.rules()
    messages = FEATURE.catalog.messages
    assert ai_access in FEATURE.operations
    for rid in AGENTIC_RULE_IDS:
        d = RULES[rid]
        assert d.category.value == "ai_readiness" and d.heuristic
        assert d.severity.value in ("Info", "Low")
        for key in (d.message_key, d.recommendation_key, d.failure_check_key):
            assert messages["en"].get(key) and messages["tr"].get(key)
    assert "llms-txt-missing" in RULES and "llms-txt-missing" not in AGENTIC_RULE_IDS


def test_llms_txt_notes() -> None:
    assert llms_txt_notes(GOOD_LLMS) == []
    assert "missing-h1" in llms_txt_notes("just some text without a title " * 5)
    assert "no-markdown-links" in llms_txt_notes("# Title\n\n> sum\n\n## S\n\nplain words only here, nothing else.")
    assert llms_txt_notes("<!doctype html><html></html>") == ["served-html"]
    assert "too-short" in llms_txt_notes("# a")
    assert "no-summary-blockquote" in llms_txt_notes(
        "# T\n\n## S\n\n- [a](https://e.com/a): x\n- [b](https://e.com/b): y\n"
    )
    assert "multiple-h1" in llms_txt_notes(GOOD_LLMS + "\n# Another\n")
    # code fences hide headings and links
    assert "missing-h1" in llms_txt_notes("```\n# not a title\n[a](https://e.com)\n```\n" + "x" * 60)


async def test_clean_site_has_only_optional_gaps_and_four_requests() -> None:
    res, ids, f = await run(
        table={
            f"{HOST}/index.md": (
                200,
                {"content-type": "text/markdown"},
                "# Acme\n\nhello world, long enough",
            ),
            f"{HOST}/.well-known/mcp": (
                200,
                {"content-type": "application/json"},
                '{"name": "acme"}',
            ),
        },
        rb=robots("User-agent: *\nContent-Signal: search=yes, ai-train=no\nAllow: /\n"),
    )
    assert ids == []
    assert status(res, "markdown-delivery") == "pass"
    assert status(res, "content-signal") == "pass"
    assert status(res, "agent-discovery") == "pass"
    assert status(res, "named-controls") == "pass"
    assert status(res, "llms-txt") == "na"  # robots module said it is absent: not judged here
    assert len(f.requested) <= MAX_REQUESTS
    assert res.data.fraction == "4/4"
    assert res.data.lighthouse_fraction is None  # never computed by us
    assert "lighthouse-fraction-needs-psi" in res.data.notes


async def test_negotiation_is_not_checked_not_guessed() -> None:
    res, _ids, _f = await run()
    assert ("markdown-negotiation", "fetcher-headers-unsupported") in {
        (n.check_id, n.reason_code) for n in res.not_checked
    }


async def test_malformed_llms_txt_is_flagged_low() -> None:
    res, ids, _ = await run(
        table={f"{HOST}/llms.txt": (200, {"content-type": "text/plain"}, "hello " * 30)},
        rb=robots(llms=True),
    )
    assert "llms-txt-malformed" in ids
    f = next(x for x in res.findings if x.id == "llms-txt-malformed")
    assert f.severity.value == "Low" and "missing-h1" in (f.evidence or "")
    assert status(res, "llms-txt") == "fail"


async def test_good_llms_txt_passes() -> None:
    res, ids, _ = await run(
        table={f"{HOST}/llms.txt": (200, {"content-type": "text/plain"}, GOOD_LLMS)},
        rb=robots(llms=True),
    )
    assert "llms-txt-malformed" not in ids and status(res, "llms-txt") == "pass"


async def test_html_catch_all_is_not_a_markdown_or_discovery_file() -> None:
    html_ct = {"content-type": "text/html"}
    res, ids, _ = await run(
        table={
            f"{HOST}/index.md": (200, html_ct, "<!doctype html><html></html>"),
            f"{HOST}/.well-known/mcp": (200, html_ct, "<html></html>"),
            f"{HOST}/.well-known/ai-plugin.json": (200, {}, "<html>nope</html>"),
        }
    )
    assert "markdown-not-available" in ids
    assert status(res, "agent-discovery") == "na"


async def test_alternate_markdown_link_counts() -> None:
    html = HOME.replace("</head>", '<link rel="alternate" type="text/markdown" href="/x.md"></head>')
    res, ids, _ = await run(html)
    assert "markdown-not-available" not in ids and status(res, "markdown-delivery") == "pass"


async def test_content_signal_missing_is_info_and_unknown_when_robots_down() -> None:
    res, ids, _ = await run()
    assert "content-signal-missing" in ids
    f = next(x for x in res.findings if x.id == "content-signal-missing")
    assert f.severity.value == "Info"
    down = RobotsReport(status="unreachable", rules=None, llms_txt_present=None, http_status=None)
    res2, ids2, _ = await run(rb=down)
    assert "content-signal-missing" not in ids2
    assert status(res2, "content-signal") == "unknown"


async def test_unnamed_controls_are_flagged() -> None:
    html = (
        "<html><body><main><button></button><button aria-label='Menu'></button>"
        "<input name='email'><input type='hidden' name='t'><input id='a'><label for='a'>A</label>"
        "<a href='/x'><img src='i.png'></a><a href='/y'><img src='i.png' alt='Home'></a>"
        "<div role='button'></div></main></body></html>"
    )
    res, ids, _ = await run(html)
    assert "agent-unfriendly-controls" in ids
    assert res.data.unnamed == {"buttons": 2, "inputs": 1, "links": 1}
    assert status(res, "named-controls") == "fail"


async def test_webmcp_traces_in_markup() -> None:
    html = HOME.replace("</body>", "<script>navigator.modelContext.registerTool({name:'x'})</script></body>")
    res, _ids, _ = await run(html)
    assert status(res, "agent-discovery") == "pass"
    assert "modelContext" in next(c.detail for c in res.data.checks if c.id == "agent-discovery")


async def test_unreachable_documents_are_unknown_not_failures() -> None:
    class Down(Scripted):
        async def fetch_bytes(self, url, *, max_bytes=0):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    res = await _call(HOME, Down(), robots(llms=True))
    assert status(res, "llms-txt") == "unknown"
    assert "llms-txt-malformed" not in [f.id for f in res.findings]
    assert "markdown-not-available" not in [f.id for f in res.findings]


async def test_request_budget_is_respected() -> None:
    res, _ids, f = await run(rb=robots(llms=True))
    assert len(f.requested) == res.data.requests <= MAX_REQUESTS


async def test_hostile_input_is_contained() -> None:
    nasty = "<html><body>" + "<button>" * 20000 + "<input " * 5000 + "</body>"
    huge_llms = "# T\n" + ("[a](" + "x" * 400 + ")\n") * 20000
    res, _ids, _ = await run(
        nasty,
        table={f"{HOST}/llms.txt": (200, {"content-type": "text/plain"}, huge_llms)},
        rb=robots(llms=True),
    )
    assert res.data.checks
    assert "oversize" in llms_txt_notes("# T\n> s\n## S\n[a](b)\n" + "x" * 200_000)
    assert llms_txt_notes("﻿" + "\r" * 100000 + "# x") is not None


async def test_robots_is_computed_when_not_given() -> None:
    pages = {
        f"{HOST}/robots.txt": (
            200,
            {"content-type": "text/plain"},
            b"User-agent: *\nContent-Signal: search=yes\nAllow: /\n",
        ),
    }
    async with fakes.session(pages) as s:
        res = await ai_access(page("/", HOME), session=s)
    assert res.ok and res.data is not None
    assert status(res, "content-signal") == "pass"
    assert status(res, "llms-txt") == "na"  # the robots operation found no llms.txt
    assert res.data.requests == 3  # robots.txt and llms.txt of the robots operation are not in the budget
    assert '"operation":"ai_access"' in res.to_json()
