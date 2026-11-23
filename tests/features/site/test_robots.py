"""robots.txt: RFC 9309 parsing/matching, status handling, AI crawler access, llms.txt (offline)."""

from __future__ import annotations

import time
from importlib.resources import files

from seoprobe.core.config import Config
from seoprobe.core.models import Severity
from seoprobe.core.net.client import RawResponse
from seoprobe.core.rules import catalog
from seoprobe.features.site import Group, RobotsReport, RobotsRules, robots
from seoprobe.features.site.robots import (
    AI_CRAWLERS_RESOURCE,
    MAX_ROBOTS_BYTES,
    can_fetch,
    is_allowed,
    parse_robots,
    product_token,
    run_robots_checks,
    select_rules,
)
from tests import fakes

BOT = "SEOAnalyzerBot/0.1"


def _fx(pages: dict[str, tuple[int, dict[str, str], bytes]]) -> fakes.FakeHttp:
    return fakes.FakeHttp(pages)


def _site(robots: str | None, *, status: int = 200, llms: tuple[int, str, bytes] | None = None):
    pages: dict[str, tuple[int, dict[str, str], bytes]] = {}
    if robots is not None:
        pages["https://example.com/robots.txt"] = (
            status,
            {"content-type": "text/plain"},
            robots.encode(),
        )
    elif status:
        pages["https://example.com/robots.txt"] = (status, {"content-type": "text/plain"}, b"")
    if llms:
        pages["https://example.com/llms.txt"] = (llms[0], {"content-type": llms[1]}, llms[2])
    return _fx(pages)


async def _run(robots: str | None, *, status: int = 200, entry: str = "https://example.com/", llms=None):
    return await run_robots_checks(
        "https://example.com",
        entry,
        _site(robots, status=status, llms=llms),  # type: ignore[arg-type]
        BOT,
    )


def _ids(res) -> list[str]:
    return [f.id for f in res.output.findings]


# ------------------------------------------------------------------ parsing / matching


def test_product_token() -> None:
    assert product_token("GPTBot") == "gptbot"
    assert product_token("GPTBot/1.1") == "gptbot"
    assert product_token("ClaudeBot*") == "claudebot"
    assert product_token("*") == "*"
    assert product_token("008") == ""


def test_named_group_replaces_star_and_groups_merge() -> None:
    r = parse_robots(
        "User-agent: *\nDisallow: /\n\nUser-agent: GPTBot\nDisallow: /a\n\nUser-agent: gptbot\nAllow: /a/ok\n"
    )
    matched, rules = select_rules(r, "GPTBot")
    assert matched == "named" and ("disallow", "/a") in rules and ("allow", "/a/ok") in rules
    assert ("disallow", "/") not in rules  # nothing inherits from *
    assert select_rules(r, "other")[0] == "star"
    assert select_rules(parse_robots("User-agent: x\nDisallow: /"), "other")[0] == "none"


def test_consecutive_user_agent_lines_share_one_group() -> None:
    r = parse_robots("User-agent: a\nUser-agent: b\nDisallow: /x\n")
    assert len(r.groups) == 1 and r.groups[0].agents == ["a", "b"]


def test_longest_match_allow_wins_tie_and_empty_disallow() -> None:
    rules = [("disallow", "/private"), ("allow", "/private/ok")]
    assert not is_allowed(rules, "/private/x") and is_allowed(rules, "/private/ok/y")
    assert is_allowed([("disallow", "/a"), ("allow", "/a")], "/a")  # tie -> allow
    assert is_allowed([("disallow", "")], "/anything")


def test_wildcards_and_end_anchor() -> None:
    rules = [("disallow", "/*.pdf$")]
    assert not is_allowed(rules, "/x/y.pdf") and is_allowed(rules, "/x/y.pdf?v=1") and is_allowed(rules, "/y.pdfx")
    assert not is_allowed([("disallow", "/a*b*c")], "/a-b-c-d")
    assert is_allowed([("disallow", "/a*b*c")], "/a-c-b")


def test_line_endings_bom_comments_and_unicode_separator() -> None:
    r = parse_robots("﻿User-agent: *\r\nDisallow: /a # c\rDisallow: /b\n# x Disallow: /evil\n")
    assert [v for _f, v in r.groups[0].rules] == [
        "/a",
        "/b",
    ]  # U+2028 inside a comment is not a line break


def test_can_fetch_uses_product_token_of_the_user_agent() -> None:
    r = parse_robots("User-agent: SEOAnalyzerBot\nDisallow: /secret\n\nUser-agent: *\nAllow: /\n")
    assert not can_fetch(r, BOT, "https://example.com/secret/x")
    assert can_fetch(r, "Googlebot", "https://example.com/secret/x")
    assert not can_fetch(r, BOT, "https://example.com/secret?x=1")


def test_query_string_is_part_of_the_matched_path() -> None:
    r = parse_robots("User-agent: *\nDisallow: /*?session=\n")
    assert not can_fetch(r, BOT, "https://example.com/p?session=1")
    assert can_fetch(r, BOT, "https://example.com/p?other=1")


# ------------------------------------------------------------------ hostile input


def test_catastrophic_pattern_is_fast() -> None:
    r = parse_robots("User-agent: *\nDisallow: /" + "*" * 5000 + "x\n")
    t = time.perf_counter()
    for _ in range(200):
        can_fetch(r, BOT, "https://example.com/" + "a" * 2000)
    assert time.perf_counter() - t < 2.0


def test_oversized_file_is_truncated_not_rejected() -> None:
    big = "User-agent: *\nDisallow: /a\n" + ("# padding\n" * 200_000)
    r = parse_robots(big)
    assert r.truncated and r.groups and r.groups[0].rules == [("disallow", "/a")]


def test_rules_after_the_byte_limit_are_ignored() -> None:
    text = "User-agent: *\n" + "# x\n" * (MAX_ROBOTS_BYTES // 4) + "Disallow: /late\n"
    assert parse_robots(text).groups[0].rules == []


def test_rule_count_is_capped() -> None:
    r = parse_robots("User-agent: *\n" + "Disallow: /a\n" * 50_000)
    assert r.truncated and len(r.groups[0].rules) == 10_000


def test_sitemap_lines_capped_and_garbage_ignored() -> None:
    r = parse_robots("\x00\x01 garbage\n" + "Sitemap: https://x/s.xml\n" * 500 + "nonsense line\n::::\n")
    assert len(r.sitemaps) == 64


# ------------------------------------------------------------------ run_robots_checks


async def test_missing_robots_means_no_restrictions() -> None:
    res = await _run(None, status=404)
    assert res.status == "missing" and "robots-missing" in _ids(res)
    f = next(x for x in res.output.findings if x.id == "robots-missing")
    assert f.severity == Severity.INFO
    assert res.allows(BOT, "https://example.com/anything")
    assert all(a.root_allowed for a in res.ai_crawlers)


async def test_server_error_is_unreachable_and_treated_as_disallow_all() -> None:
    res = await _run("boom", status=503)
    assert res.status == "unreachable" and _ids(res) == ["robots-unreachable", "llms-txt-missing"]
    assert not res.allows(BOT, "https://example.com/")  # RFC 9309: assume complete disallow
    assert {n.reason_code for n in res.output.not_checked} >= {"robots-unreachable"}
    assert res.ai_crawlers == []  # not judged: no robots.txt was read
    assert "robots-blocks-site" not in _ids(res)  # not a block the site chose


async def test_network_error_is_unreachable() -> None:
    class Down:
        async def fetch_bytes(self, url: str, *, max_bytes: int | None = None) -> RawResponse:
            return RawResponse(url, url, None, {}, b"", False, "timeout", [])

    res = await run_robots_checks("https://example.com", "https://example.com/", Down(), BOT)  # type: ignore[arg-type]
    assert res.status == "unreachable" and "robots-unreachable" in _ids(res)


async def test_disallow_all_blocks_site() -> None:
    res = await _run("User-agent: *\nDisallow: /\n")
    assert "robots-blocks-site" in _ids(res) and "robots-disallows-entry" in _ids(res)
    entry = next(f for f in res.output.findings if f.id == "robots-disallows-entry")
    assert entry.severity == Severity.HIGH and "Googlebot" in (entry.evidence or "")
    assert not res.entry_allowed_for_googlebot and not res.entry_allowed_for_bot


async def test_only_our_bot_blocked_is_info_not_high() -> None:
    res = await _run("User-agent: SEOAnalyzerBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    f = next(x for x in res.output.findings if x.id == "robots-disallows-entry")
    assert f.severity == Severity.INFO and f.severity_original == Severity.HIGH and f.severity_reason
    assert "robots-blocks-site" not in _ids(res)
    assert res.entry_allowed_for_googlebot and not res.entry_allowed_for_bot


async def test_partial_block_is_not_a_site_block() -> None:
    res = await _run("User-agent: *\nDisallow: /\nAllow: /public\n", entry="https://example.com/public/x")
    assert "robots-blocks-site" not in _ids(res) and "robots-disallows-entry" not in _ids(res)


async def test_ai_search_blocked_vs_training_blocked() -> None:
    res = await _run(
        "User-agent: OAI-SearchBot\nDisallow: /\n\nUser-agent: GPTBot\nDisallow: /\n\n"
        "User-agent: Google-Extended\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    )
    ids = _ids(res)
    assert "ai-search-bots-blocked" in ids and "ai-training-bots-blocked" in ids
    search = next(f for f in res.output.findings if f.id == "ai-search-bots-blocked")
    train = next(f for f in res.output.findings if f.id == "ai-training-bots-blocked")
    assert "OAI-SearchBot" in (search.evidence or "") and "GPTBot" not in (search.evidence or "")
    assert "GPTBot" in (train.evidence or "") and "Google-Extended" in (train.evidence or "")
    assert search.category.value == "ai_readiness" and train.severity == Severity.INFO


async def test_training_block_alone_does_not_report_search_block() -> None:
    res = await _run("User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    assert "ai-search-bots-blocked" not in _ids(res) and "ai-training-bots-blocked" in _ids(res)


async def test_wildcard_group_blocking_everything_blocks_ai_search_too() -> None:
    res = await _run("User-agent: *\nDisallow: /\n")
    assert "ai-search-bots-blocked" in _ids(res)


async def test_ai_status_records_capability_and_caveat() -> None:
    res = await _run("User-agent: *\nAllow: /\n")
    by = {a.token: a for a in res.ai_crawlers}
    assert by["GPTBot"].capability == "training" and "ChatGPT Search" in by["GPTBot"].not_evidence_for
    assert by["OAI-SearchBot"].capability == "search"
    assert by["Google-Extended"].not_evidence_for.startswith("Google Search")
    assert by["Claude-SearchBot"].group == "star"


async def test_llms_txt_present_missing_and_html_catch_all() -> None:
    good = await _run(
        "User-agent: *\nAllow: /\n",
        llms=(200, "text/plain", b"# Site\n> summary\n- [a](https://x/a)"),
    )
    assert good.llms_txt_present is True and "llms-txt-missing" not in _ids(good)
    none = await _run("User-agent: *\nAllow: /\n")
    assert none.llms_txt_present is False and "llms-txt-missing" in _ids(none)
    catch_all = await _run("User-agent: *\nAllow: /\n", llms=(200, "text/html; charset=utf-8", b"<html>home</html>"))
    assert catch_all.llms_txt_present is False and "llms-txt-missing" in _ids(catch_all)
    f = next(x for x in none.output.findings if x.id == "llms-txt-missing")
    assert f.severity == Severity.INFO and f.heuristic and "Google Search ignores" in f.message


async def test_sitemap_lines_are_extracted_deduplicated_and_validated() -> None:
    res = await _run(
        "User-agent: *\nAllow: /\nSitemap: https://example.com/s.xml\nSitemap: https://example.com/s.xml\n"
        "Sitemap: javascript:alert(1)\nSitemap: ftp://x/s.xml\nSitemap: https://cdn.example.net/s2.xml\n"
    )
    assert res.sitemaps == ["https://example.com/s.xml", "https://cdn.example.net/s2.xml"]


async def test_huge_robots_is_truncated_and_still_judged() -> None:
    body = "User-agent: *\nDisallow: /secret\n" + "# pad\n" * 400_000
    res = await _run(body)
    assert res.rules.truncated and not res.allows(BOT, "https://example.com/secret")


async def test_findings_use_registry_texts() -> None:
    res = await _run("User-agent: *\nDisallow: /\n")
    for f in res.output.findings:
        d = catalog.rules()[f.id]
        assert f.message == catalog.message(d.message_key, "en") and f.scope == "site"


def test_ai_crawler_data_file_is_wellformed_and_dated_from_the_source() -> None:
    import json

    data = json.loads(files("seoprobe.core").joinpath(AI_CRAWLERS_RESOURCE).read_text("utf-8"))
    assert data["last_verified"] == "2026-09-23" and "agentic_check.py" in data["last_verified_source"]
    for c in data["crawlers"]:
        assert {"token", "vendor", "capability", "governs", "not_evidence_for", "source"} <= set(c)
        assert c["capability"] in {"search", "training", "grounding", "user-agent", "ads"}
    toks = {c["token"] for c in data["crawlers"]}
    assert {
        "GPTBot",
        "OAI-SearchBot",
        "ClaudeBot",
        "Claude-SearchBot",
        "PerplexityBot",
        "Google-Extended",
        "Applebot-Extended",
    } <= toks


# ------------------------------------------------------------------ the operation and RobotsReport

ROBOTS_TXT = (
    "User-agent: *\nDisallow: /private\n\nUser-agent: seoprobe\nDisallow: /\nSitemap: https://example.com/s.xml\n"
)


def _pages(robots_txt: str | None, status: int = 200) -> dict[str, tuple[int, dict[str, str], bytes]]:
    if robots_txt is None:
        return {"https://example.com/robots.txt": (status, {"content-type": "text/plain"}, b"")}
    return {"https://example.com/robots.txt": (status, {"content-type": "text/plain"}, robots_txt.encode())}


async def test_robots_operation_matches_run_robots_checks() -> None:
    async with fakes.session(_pages(ROBOTS_TXT)) as s:
        res = await robots("https://example.com/page", session=s)
    direct = await run_robots_checks(
        "https://example.com",
        "https://example.com/page",
        fakes.FakeHttp(_pages(ROBOTS_TXT)),
        Config().bot_name,  # type: ignore[arg-type]
    )
    assert res.ok and isinstance(res.data, RobotsReport)
    rep = res.data
    assert rep.status == direct.status == "ok" and rep.available
    assert rep.http_status == 200 and rep.sitemaps == ["https://example.com/s.xml"]
    assert rep.rules == direct.rules and rep.ai_crawlers == direct.ai_crawlers
    assert not rep.entry_allowed_for_bot and rep.entry_allowed_for_googlebot
    assert rep.llms_txt_present is False and rep.facts == direct.output.facts
    assert [f.id for f in res.findings] == [f.id for f in direct.output.findings]
    assert res.not_checked == direct.output.not_checked
    assert '"available":true' in res.to_json()


async def test_robots_operation_origin_override() -> None:
    pages = {"https://other.example/robots.txt": (200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /\n")}
    async with fakes.session(pages) as s:
        res = await robots("https://example.com/", origin="https://other.example", session=s)
    assert res.ok and res.data is not None and res.data.status == "ok"
    assert "https://other.example/robots.txt" in s.http.requested  # type: ignore[attr-defined]


async def test_report_allows_reproduces_robots_allows() -> None:
    async with fakes.session(_pages(ROBOTS_TXT)) as s:
        rep = (await robots("https://example.com/", session=s)).unwrap()
    assert not rep.allows("https://example.com/x", "seoprobe/0.1")
    assert rep.allows("https://example.com/x", "Googlebot")
    assert not rep.allows("https://example.com/private/a", "Googlebot")
    # unreachable robots.txt: allowed (the source robots_allows), unlike RFC 9309 can_fetch
    async with fakes.session(_pages("boom", status=503)) as s:
        down = (await robots("https://example.com/", session=s)).unwrap()
    assert not down.available and down.allows("https://example.com/x", "Googlebot")
    assert down.rules is not None and not can_fetch(down.rules, "Googlebot", "https://example.com/x")
    # no rules at all: allowed
    assert RobotsReport(status="ok").allows("https://example.com/", "Googlebot")


def test_report_models_round_trip() -> None:
    rep = RobotsReport(status="ok", rules=RobotsRules(groups=[Group(agents=["*"], rules=[("disallow", "/a")])]))
    back = RobotsReport.model_validate_json(rep.model_dump_json())
    assert back == rep and back.available and not back.allows("https://example.com/a", "x")
