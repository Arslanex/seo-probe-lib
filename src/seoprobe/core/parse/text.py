"""Text hygiene for page-derived strings."""

from __future__ import annotations

import re
import unicodedata
import uuid

_BIDI = {"‪", "‫", "‬", "‭", "‮", "⁦", "⁧", "⁨", "⁩"}


def clean_text(text: str) -> str:
    """Strip control and bidi characters from page-derived text."""
    out = []
    for ch in text:
        if ch in _BIDI:
            continue
        if unicodedata.category(ch) in ("Cc", "Cf") and ch not in ("\n", "\t"):
            continue
        out.append(ch)
    return "".join(out)


_META_CHARSET = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([a-zA-Z0-9_\-:.]+)", re.I)
_CT_CHARSET = re.compile(r"charset\s*=\s*[\"']?([^;,\"'\s>]+)", re.I)


def decode_body(body: bytes, content_type: str | None) -> str:
    """BOM -> Content-Type charset -> <meta> prescan -> utf-8."""
    for bom, enc in (
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
    ):
        if body.startswith(bom):
            return body.decode(enc, errors="replace")
    candidates: list[str] = []
    if content_type and (m := _CT_CHARSET.search(content_type)):
        candidates.append(m.group(1))
    if m2 := _META_CHARSET.search(body[:4096]):
        candidates.append(m2.group(1).decode("ascii", "ignore"))
    candidates.append("utf-8")
    for enc in candidates:
        try:
            return body.decode(enc.strip(), errors="replace")
        except LookupError:
            continue
    return body.decode("utf-8", errors="replace")


def new_id() -> str:
    """A short random id for audits and pages."""
    return uuid.uuid4().hex[:16]
