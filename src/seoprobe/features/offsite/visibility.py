"""
Visibility signals: public, keyless, informational.

None of these is proof of indexing or a ranking estimate, and none is part of the score:
* RDAP says how old the registration is (a young domain needs time),
* Wayback says how long the site has been archivable,
* Common Crawl says whether a large public crawl knows the site (a proxy, not Google).
Whether Google indexed a page is only answerable through Search Console (not connected).
``notes`` holds stable CODES; the report translates them.

RDAP: TLD -> RDAP server comes from the IANA bootstrap file (cached per process). A TLD missing
from the bootstrap (for example ``.tr``) yields ``error="rdap-unsupported-tld"``.

Registrable-domain extraction is deliberately simple: the last two labels, or three when the
last two form a known multi-label suffix (small list below). The Public Suffix List is NOT used
and this is not verified against it; an unlisted multi-label suffix would query the wrong name
and normally come back as ``http-404``.

Wayback: two requests (first and last capture need two orderings): the oldest unique URLs (also
the sample count, capped at 100, meaning "at least") and the newest capture. No captures is a
valid answer (``urls_archived_sample == 0``), not an error.

Common Crawl: is the domain in the newest index (CDX server)? A *proxy* for "known to a large
crawler"; it says nothing about Google's index. A 404 / "no captures" answer is ``present=False``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlparse

from seoprobe.core.feature import TIME_BUDGET, Context, Outcome, operation
from seoprobe.core.models import Requirements
from seoprobe.core.net.api import Session, get_json, get_text, host_of

from .models import VisibilitySignals

# ------------------------------------------------------------------ RDAP

BOOTSTRAP = "https://data.iana.org/rdap/dns.json"
MULTI_LABEL_SUFFIXES = frozenset(
    {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk",
        "com.tr", "org.tr", "gov.tr", "net.tr", "edu.tr", "gen.tr", "web.tr",
        "com.au", "net.au", "org.au", "edu.au", "gov.au",
        "co.nz", "org.nz", "co.jp", "or.jp", "ne.jp", "co.in", "net.in", "org.in",
        "com.br", "net.br", "org.br", "co.za", "org.za", "com.mx", "com.ar", "com.cn", "com.hk", "com.sg",
    }
)  # fmt: skip

_bootstrap_cache: dict[str, str] = {}  # tld -> RDAP base URL


def registrable_domain(host: str) -> str:
    labels = [x for x in host_of(host).split(".") if x]
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _parse_bootstrap(data: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for svc in data.get("services", []) if isinstance(data, dict) else []:
        try:
            tlds, urls = svc[0], svc[1]
        except (IndexError, TypeError):
            continue
        base = next((u for u in urls if isinstance(u, str) and u.startswith("https://")), None)
        if base:
            for t in tlds:
                out[str(t).lower()] = base
    return out


def _registrar(data: dict[str, Any]) -> str | None:
    for ent in data.get("entities", []) or []:
        if "registrar" in (ent.get("roles") or []):
            for item in (ent.get("vcardArray") or [None, []])[1] or []:
                if isinstance(item, list) and len(item) >= 4 and item[0] == "fn":
                    return str(item[3])
    return None


async def lookup_rdap(domain: str, session: Session, *, timeout_s: float = 30.0) -> dict[str, Any]:
    out: dict[str, Any] = {
        "created": None,
        "age_days": None,
        "registrar": None,
        "registry_host": None,
        "error": None,
    }
    reg = registrable_domain(domain)
    tld = reg.rsplit(".", 1)[-1] if "." in reg else ""
    if not tld:
        out["error"] = "invalid-domain"
        return out
    if not _bootstrap_cache:
        data, err = await get_json(session, BOOTSTRAP, timeout_s=timeout_s)
        if err:
            out["error"] = f"bootstrap-{err}"
            return out
        _bootstrap_cache.update(_parse_bootstrap(data))
    base = _bootstrap_cache.get(tld)
    if base is None:
        out["error"] = "rdap-unsupported-tld"
        return out
    out["registry_host"] = urlparse(base).hostname
    data, err = await get_json(session, f"{base.rstrip('/')}/domain/{reg}", timeout_s=timeout_s)
    if err:
        out["error"] = err
        return out
    if not isinstance(data, dict):
        out["error"] = "bad-json"
        return out
    out["registrar"] = _registrar(data)
    for ev in data.get("events", []) or []:
        if isinstance(ev, dict) and ev.get("eventAction") == "registration":
            try:
                dt = datetime.fromisoformat(str(ev["eventDate"]).replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            out["created"] = dt.date().isoformat()
            out["age_days"] = max(0, (datetime.now(UTC) - dt).days)
            break
    if out["created"] is None:
        out["error"] = "no-registration-event"
    return out


# ------------------------------------------------------------------ Wayback

CDX = "https://web.archive.org/cdx/search/cdx"
WAYBACK_SAMPLE_LIMIT = 100
WAYBACK_SAMPLE_TIMEOUT_S = 12.0  # the domain-wide URL sample; the capture dates never wait for it


def _date(ts: str) -> str | None:
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 and ts[:8].isdigit() else None


async def _cdx(session: Session, url: str, timeout_s: float) -> tuple[Any, str | None]:
    """CDX answers an empty BODY (not ``[]``) when nothing matches: that is "no captures", not bad JSON."""
    status, text, err = await get_text(session, url, timeout_s=timeout_s)
    if err:
        return None, err
    if status != 200:
        return None, f"http-{status}"
    if not (text or "").strip():
        return [], None
    try:
        return json.loads(text or ""), None
    except ValueError:
        return None, "bad-json"


def _rows(data: Any) -> list[list[str]]:
    """CDX JSON: first row is the header; an empty result is [] or None."""
    if not isinstance(data, list) or len(data) < 2:
        return []
    return [r for r in data[1:] if isinstance(r, list) and r]


async def lookup_wayback(domain: str, session: Session, *, timeout_s: float = 30.0) -> dict[str, Any]:
    """
    First and last capture of the host, plus a sample of the domain's archived URLs.

    The dates come from two exact-host queries (``limit=1`` and ``limit=-1``), which answer in
    about a second. The URL sample needs a domain-wide query that takes 10-50 s on a big site, so
    it runs alongside with its own shorter limit: when it does not answer, the dates stay valid,
    ``urls_archived_sample`` is None and ``error`` is ``sample-<code>``.
    """
    out: dict[str, Any] = {
        "first_capture": None,
        "last_capture": None,
        "captures": 0,
        "urls_archived_sample": 0,
        "error": None,
    }
    host = host_of(domain)
    if not host:
        out["error"] = "invalid-domain"
        return out
    host_q = f"{CDX}?url={quote(host)}&output=json&fl=timestamp"
    sample_q = (
        f"{CDX}?url={quote(host)}&matchType=domain&output=json&fl=timestamp,original"
        f"&collapse=urlkey&limit={WAYBACK_SAMPLE_LIMIT}"
    )
    (first, first_err), (last, last_err), (sample, sample_err) = await asyncio.gather(
        _cdx(session, f"{host_q}&limit=1", timeout_s=timeout_s),
        _cdx(session, f"{host_q}&limit=-1", timeout_s=timeout_s),
        _cdx(session, sample_q, timeout_s=min(timeout_s, WAYBACK_SAMPLE_TIMEOUT_S)),
    )
    if first_err and last_err and sample_err:
        out["error"] = first_err
        return out
    sample_rows = _rows(sample)
    first_rows, last_rows = _rows(first), _rows(last)
    stamps = sorted(r[0] for r in sample_rows if isinstance(r[0], str))
    if first_rows and isinstance(first_rows[0][0], str):
        out["first_capture"] = _date(first_rows[0][0])
    elif stamps:  # the host itself was never captured, another URL of the domain was
        out["first_capture"] = _date(stamps[0])
    if last_rows and isinstance(last_rows[0][0], str):
        out["last_capture"] = _date(last_rows[0][0])
    if sample_err:
        out["urls_archived_sample"] = out["captures"] = None
    else:
        out["urls_archived_sample"] = out["captures"] = min(len(sample_rows), WAYBACK_SAMPLE_LIMIT)
    for part, err in (("first-capture", first_err), ("last-capture", last_err), ("sample", sample_err)):
        if err:  # what did answer stays valid; the code says which part did not
            out["error"] = f"{part}-{err}"
            break
    return out


# ------------------------------------------------------------------ Common Crawl

COLLINFO = "https://index.commoncrawl.org/collinfo.json"
CC_SAMPLE_LIMIT = 50


async def lookup_commoncrawl(domain: str, session: Session, *, timeout_s: float = 30.0) -> dict[str, Any]:
    out: dict[str, Any] = {
        "collection": None,
        "present": None,
        "pages_seen_sample": 0,
        "error": None,
    }
    host = host_of(domain)
    if not host:
        out["error"] = "invalid-domain"
        return out
    info, err = await get_json(session, COLLINFO, timeout_s=timeout_s)
    if err:
        out["error"] = f"collinfo-{err}"
        return out
    if not isinstance(info, list) or not info or not isinstance(info[0], dict) or not info[0].get("cdx-api"):
        out["error"] = "bad-json"
        return out
    out["collection"] = info[0].get("id")
    api = str(info[0]["cdx-api"])
    status, text, err = await get_text(
        session,
        f"{api}?url={quote(host)}/*&output=json&limit={CC_SAMPLE_LIMIT}&fl=url",
        timeout_s=timeout_s,
    )
    if err:
        out["error"] = err
        return out
    if status == 404:
        out["present"] = False  # "No Captures found" arrives as a 404 with a JSON message
        return out
    if status != 200:  # a 5xx is a server problem, never "the site is absent"
        out["error"] = f"http-{status}"
        return out
    if not (text or "").strip():
        out["present"] = False
        return out
    urls = 0
    for line in (text or "").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("url"):
            urls += 1
    out["present"] = urls > 0
    out["pages_seen_sample"] = min(urls, CC_SAMPLE_LIMIT)
    return out


# ------------------------------------------------------------------ collection


async def _safe(coro: Any, deadline: float | None = None) -> dict[str, Any]:
    try:
        async with asyncio.timeout_at(deadline):
            res = await coro
    except TimeoutError:  # the time budget ran out: this source is not checked, the others keep theirs
        return {"error": TIME_BUDGET}
    except Exception as exc:  # noqa: BLE001 - one source failing must never break the others
        return {"error": f"internal-error: {type(exc).__name__}"}
    return dict(res)


def notes_for(v: VisibilitySignals) -> list[str]:
    notes: list[str] = []
    age = v.rdap.get("age_days")
    if isinstance(age, int):
        if age < 180:
            notes.append("domain-under-180-days")
        elif age < 365:
            notes.append("domain-under-1-year")
    if not v.wayback.get("error") and v.wayback.get("urls_archived_sample") == 0:
        notes.append("no-wayback-captures")
    if v.common_crawl.get("present") is False:
        notes.append("not-in-common-crawl")
    notes.append("gsc-not-connected")
    return notes


async def collect_visibility(
    domain: str, session: Session, *, timeout_s: float = 30.0, deadline: float | None = None
) -> VisibilitySignals:
    """
    The three lookups side by side on ``session`` (never closed here). Never raises. A lookup
    still running at ``deadline`` (event-loop time) gets ``error="time-budget"``.
    """
    host = host_of(domain)
    rdap, wb, cc = await asyncio.gather(
        _safe(lookup_rdap(registrable_domain(host), session, timeout_s=timeout_s), deadline),
        _safe(lookup_wayback(host, session, timeout_s=timeout_s), deadline),
        _safe(lookup_commoncrawl(host, session, timeout_s=timeout_s), deadline),
    )
    v = VisibilitySignals(domain=host, rdap=rdap, wayback=wb, common_crawl=cc, search_console={"connected": False})
    v.notes = notes_for(v)
    return v


@operation("visibility", output=VisibilitySignals, requires=Requirements(network=True), timeout_s=120)
async def visibility(ctx: Context, domain: str, *, budget_s: float | None = None) -> Outcome[VisibilitySignals]:
    """
    RDAP registration age, Wayback captures and Common Crawl presence of ``domain`` (a host or
    URL). Informational: a failing lookup is an ``error`` field of its section, never an error.

    ``budget_s``: seconds the lookups may take (default ``Config.visibility_budget_s``;
    ``math.inf``: as long as the operation's timeout allows). A lookup cut short keeps
    ``error="time-budget"``, the others keep their answers, and the result is ``partial``.
    """
    budget = budget_s if budget_s is not None else ctx.config.visibility_budget_s
    v = await collect_visibility(domain, ctx.http.api_session(), deadline=ctx.budget_deadline(budget))
    cut = [name for name in ("rdap", "wayback", "common_crawl") if getattr(v, name).get("error") == TIME_BUDGET]
    nc = [ctx.not_checked("visibility", TIME_BUDGET)] if cut else []
    return Outcome(data=v, not_checked=nc, partial=bool(cut))
