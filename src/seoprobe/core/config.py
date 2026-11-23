"""
Config: every setting of the library in one frozen object. Nothing here reads the environment
unless ``Config.from_env()`` is called explicitly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal


def package_version() -> str:
    try:
        return version("seoprobe")
    except PackageNotFoundError:  # running from a source tree without an install
        return "0.0.0"


@dataclass(frozen=True)
class GoogleApp:
    """OAuth client of the Google Cloud project (Search Console and GA4)."""

    client_id: str
    client_secret: str = field(repr=False)


@dataclass(frozen=True)
class Config:
    lang: Literal["en", "tr"] = "en"
    bot_name: str = "seoprobe"
    bot_info_url: str = "https://github.com/Arslanex/seo-probe-lib"
    fetch_timeout_s: float = 20.0
    per_host_concurrency: int = 2
    request_delay_s: float = 0.4
    max_pages: int = 25
    max_pages_cap: int = 200
    max_external_link_checks: int = 40
    max_internal_link_checks: int = 60
    link_check_concurrency: int = 4
    # seconds the link-health probes of one analysis may take; None: as long as the operation's
    # timeout allows. A call can override it (``audit_page(..., link_check_budget_s=...)``).
    link_check_budget_s: float | None = None
    # the same for the public lookups of ``visibility`` (Wayback, Common Crawl, RDAP): informational,
    # often slow; a call overrides it (``audit_page(..., visibility_budget_s=...)``)
    visibility_budget_s: float | None = None
    max_batch_urls: int = 1000
    max_connections: int = 100
    max_response_bytes: int = 10 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    browser_concurrency: int = 2
    render: bool = True
    visibility: bool = True
    gsc_inspect_limit: int = 10
    google_api_key: str | None = field(default=None, repr=False)
    openpagerank_key: str | None = field(default=None, repr=False)
    bing_api_key: str | None = field(default=None, repr=False)
    google_app: GoogleApp | None = None

    @property
    def user_agent(self) -> str:
        return f"Mozilla/5.0 (compatible; {self.bot_name}/{package_version()}; +{self.bot_info_url})"

    @property
    def api_user_agent(self) -> str:
        return f"{self.bot_name}/{package_version()} (+{self.bot_info_url})"

    @classmethod
    def from_env(cls, prefix: str = "SEOPROBE_") -> Config:
        """Build a Config from ``<prefix><FIELD_NAME>`` variables; unset ones keep their default."""
        values: dict[str, Any] = {}
        for f in fields(cls):
            if f.name == "google_app":
                continue
            raw = os.environ.get(prefix + f.name.upper())
            if raw is None or raw == "":
                continue
            kind = str(f.type).split("|")[0].strip()  # "float | None" -> "float" (annotations are strings)
            if kind == "bool":
                values[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif kind == "int":
                values[f.name] = int(raw)
            elif kind == "float":
                values[f.name] = float(raw)
            else:
                values[f.name] = raw
        cid = os.environ.get(prefix + "GOOGLE_CLIENT_ID")
        secret = os.environ.get(prefix + "GOOGLE_CLIENT_SECRET")
        if cid and secret:
            values["google_app"] = GoogleApp(cid, secret)
        return cls(**values)
