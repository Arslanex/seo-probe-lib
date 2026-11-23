"""
JSON-LD checks (category: schema). Every rule is grounded in the upstream project references:

* ``skills/seo/references/schema-types.md``: deprecated types, the FAQPage decision, the
  validation checklist (https @context, valid @type, no placeholder text, absolute URLs, ISO
  8601 dates) and the "key properties" table;
* ``scripts/schema_ecommerce_validate.py``: the merchant-listing minimum (name, image, offers)
  and the recommended offer fields.

Wording rule: a "key property" is what the reference lists, never "Google requires" unless the
reference says so. JSON syntax errors are ``invalid-jsonld`` in the page checks, not here.
Types the reference calls ambiguous (ClaimReview, Dataset, Course) are deliberately not flagged.
"""

from __future__ import annotations

import re
from typing import Any

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import CheckOutput, Finding, Requirements
from seoprobe.core.page import Page
from seoprobe.core.parse.html import ParsedPage, trim
from seoprobe.core.rules.catalog import make_finding

from ..models import SchemaReport

_MAX_DEPTH = 8
_MAX_NODES = 2000

# Types whose Google rich result was retired or removed (schema-types.md "Deprecated: Never
# recommend"; the validator's list agrees). Ambiguous ones are excluded on purpose.
DEPRECATED_TYPES: dict[str, str] = {
    "HowTo": "September 2023",
    "SpecialAnnouncement": "July 2025",
    "CourseInfo": "June 2025",
    "EstimatedSalary": "June 2025",
    "LearningVideo": "June 2025",
    "VehicleListing": "June 2025",
}

# "Key Properties" column of schema-types.md (recommended, not guaranteed requirements).
KEY_PROPERTIES: dict[str, tuple[str, ...]] = {
    "Organization": ("name", "url", "logo"),
    "LocalBusiness": ("name", "address", "telephone"),
    "WebSite": ("name", "url"),
    "Article": ("headline", "author", "datePublished", "image"),
    "BlogPosting": ("headline", "author", "datePublished", "image"),
    "NewsArticle": ("headline", "author", "datePublished", "image"),
    "BreadcrumbList": ("itemListElement",),
    "Person": ("name",),
}
PRODUCT_MIN = ("name", "image", "offers")  # schema_ecommerce_validate._REQUIRED_PRODUCT_FIELDS
OFFER_RECOMMENDED = ("price", "priceCurrency", "availability")

_DATE_KEYS = (
    "datePublished",
    "dateModified",
    "dateCreated",
    "datePosted",
    "startDate",
    "endDate",
    "uploadDate",
    "validFrom",
    "validThrough",
    "priceValidUntil",
)
_URL_KEYS = ("url", "image", "logo", "contentUrl", "thumbnailUrl", "sameAs")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")
# Bracketed words ("[Business Name]"), lorem ipsum and "your company" style fillers. A bare
# example.com is deliberately NOT a placeholder here: it is a real, auditable domain.
_PLACEHOLDER = re.compile(
    r"\[[A-Za-z][^\]\n]{0,59}\]|lorem ipsum|\byour (?:company|business|name|website)\b",
    re.I,
)

SCHEMA_RULE_IDS: tuple[str, ...] = (
    "schema-deprecated-type",
    "schema-faq-rich-result-retired",
    "schema-placeholder-values",
    "schema-context-not-https",
    "schema-missing-type",
    "schema-missing-key-properties",
    "schema-product-missing-fields",
    "schema-offer-missing-fields",
    "schema-relative-url",
    "schema-invalid-date",
)


def _types_of(node: dict[str, Any]) -> list[str]:
    t = node.get("@type")
    return [v for v in (t if isinstance(t, list) else [t]) if isinstance(v, str)]


def _walk(data: Any, out: list[dict[str, Any]], depth: int = 0) -> None:
    """Collect every dict node (the block itself, @graph members and nested entities)."""
    if depth > _MAX_DEPTH or len(out) >= _MAX_NODES:
        return
    if isinstance(data, list):
        for item in data[:500]:
            _walk(item, out, depth + 1)
    elif isinstance(data, dict):
        out.append(data)
        for value in data.values():
            if isinstance(value, (dict, list)):
                _walk(value, out, depth + 1)


def _strings(value: Any, depth: int = 0) -> list[str]:
    if depth > 4:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value[:50] for s in _strings(v, depth + 1)]
    if isinstance(value, dict):
        return [s for k, v in list(value.items())[:50] if not k.startswith("@") for s in _strings(v, depth + 1)]
    return []


def _present(node: dict[str, Any], prop: str) -> bool:
    v = node.get(prop)
    return v not in (None, "", [], {})


def run_schema_checks(parsed: ParsedPage, url: str) -> CheckOutput:
    """Schema findings for one page. Never raises on odd JSON-LD shapes."""
    findings: list[Finding] = []
    try:
        findings = _check(parsed, url)
    except Exception:  # noqa: BLE001 - odd JSON-LD must not break the audit
        findings = []
    return CheckOutput(
        findings=findings,
        facts={
            "schema_types": list(parsed.schema_types),
            "jsonld_blocks": parsed.jsonld_total,
        },
    )


def _check(parsed: ParsedPage, url: str) -> list[Finding]:
    deprecated: list[str] = []
    faq = False
    placeholders: list[str] = []
    http_context = 0
    missing_type = 0
    key_missing: list[str] = []
    product_missing: list[str] = []
    offer_missing: list[str] = []
    relative: list[str] = []
    bad_dates: list[str] = []

    for block in parsed.jsonld:
        if block.error or block.data is None:
            continue  # syntax errors belong to invalid-jsonld in the page checks
        roots = block.data if isinstance(block.data, list) else [block.data]
        for root in roots:
            if not isinstance(root, dict):
                continue
            ctx = root.get("@context")
            for c in ctx if isinstance(ctx, list) else [ctx]:
                if isinstance(c, str) and c.strip().lower().rstrip("/") == "http://schema.org":
                    http_context += 1
            if not _types_of(root) and "@graph" not in root:
                missing_type += 1

        # Key properties are judged on the block's own entities (roots and @graph members);
        # nested references (publisher, author, ...) legitimately carry only a few fields.
        top_ids: set[int] = set()
        for root in roots:
            if isinstance(root, dict):
                top_ids.add(id(root))
                graph = root.get("@graph")
                for member in graph if isinstance(graph, list) else []:
                    if isinstance(member, dict):
                        top_ids.add(id(member))
        nodes: list[dict[str, Any]] = []
        _walk(block.data, nodes)
        for node in nodes:
            types = _types_of(node)
            for t in types:
                if t in DEPRECATED_TYPES and t not in deprecated:
                    deprecated.append(t)
                if t == "FAQPage":
                    faq = True
                if t in KEY_PROPERTIES and id(node) in top_ids:
                    miss = [p for p in KEY_PROPERTIES[t] if not _present(node, p)]
                    if miss:
                        key_missing.append(f"{t}: {', '.join(miss)}")
                if t == "Product":
                    miss = [p for p in PRODUCT_MIN if not _present(node, p)]
                    if miss:
                        product_missing.append(f"Product: {', '.join(miss)}")
                if t == "Offer":
                    miss = [p for p in OFFER_RECOMMENDED if not _present(node, p)]
                    if miss:
                        offer_missing.append(f"Offer: {', '.join(miss)}")
            for key in _DATE_KEYS:
                v = node.get(key)
                if isinstance(v, str) and v.strip() and not _ISO_DATE.match(v.strip()):
                    bad_dates.append(f"{key}={v.strip()[:30]}")
            for key in _URL_KEYS:
                if key in node:
                    for s in _strings(node[key]):
                        s = s.strip()
                        if s and not re.match(r"^https?://", s, re.I):
                            relative.append(f"{key}={s[:40]}")
            if len(placeholders) < 5:
                for s in _strings({k: v for k, v in node.items() if not isinstance(v, dict)}):
                    if m := _PLACEHOLDER.search(s):
                        placeholders.append(m.group(0)[:40])
                        if len(placeholders) >= 5:
                            break

    out: list[Finding] = []

    def add(cid: str, evidence: object, count: int | None = None) -> None:
        out.append(make_finding(cid, url=url, evidence=trim(evidence), count=count))

    if deprecated:
        add(
            "schema-deprecated-type",
            ", ".join(f"{t} (retired {DEPRECATED_TYPES[t]})" for t in deprecated),
            len(deprecated),
        )
    if faq:
        add("schema-faq-rich-result-retired", "FAQPage")
    if placeholders:
        add("schema-placeholder-values", "; ".join(dict.fromkeys(placeholders)), len(placeholders))
    if http_context:
        add("schema-context-not-https", f"{http_context} block(s) with http://schema.org", http_context)
    if missing_type:
        add("schema-missing-type", f"{missing_type} block(s) without @type", missing_type)
    if key_missing:
        add("schema-missing-key-properties", "; ".join(dict.fromkeys(key_missing)), len(key_missing))
    if product_missing:
        add("schema-product-missing-fields", "; ".join(dict.fromkeys(product_missing)), len(product_missing))
    if offer_missing:
        add("schema-offer-missing-fields", "; ".join(dict.fromkeys(offer_missing)), len(offer_missing))
    if relative:
        add("schema-relative-url", "; ".join(dict.fromkeys(relative[:5])), len(relative))
    if bad_dates:
        add("schema-invalid-date", "; ".join(dict.fromkeys(bad_dates[:5])), len(bad_dates))
    return out


@operation("schema", output=SchemaReport, requires=Requirements(network=True), timeout_s=30)
async def schema(ctx: Context, target: str | Page) -> Outcome[SchemaReport]:
    """JSON-LD detection and validation: deprecated types, placeholders, missing key fields."""
    page = await ctx.page(target)
    parsed = page.parsed
    if parsed is None:
        return Outcome(data=SchemaReport(url=page.url, checked=False))
    out = run_schema_checks(parsed, page.url)
    return Outcome(
        data=SchemaReport(
            url=page.url,
            checked=True,
            types=list(parsed.schema_types),
            blocks=parsed.jsonld_total,
            facts=out.facts,
        ),
        findings=[f if f.url else f.model_copy(update={"url": page.url}) for f in out.findings],
        not_checked=[n if n.url else n.model_copy(update={"url": page.url}) for n in out.not_checked],
    )
