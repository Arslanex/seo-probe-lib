"""Output models of the content feature."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# --- from readability.py


class KeywordCheck(BaseModel):
    keyword: str
    in_title: bool
    in_h1: bool
    in_description: bool
    in_url: bool
    in_first_100_words: bool
    in_subheadings: bool
    occurrences: int
    density_percent: float


class ReadabilityData(BaseModel):
    language: str | None = None
    readability: dict[str, Any] | None = None
    terms: list[dict[str, Any]] = Field(default_factory=list)
    phrases: list[dict[str, Any]] = Field(default_factory=list)
    title_terms: list[dict[str, Any]] = Field(default_factory=list)  # top terms also in title/H1/desc
    keywords: list[KeywordCheck] = Field(default_factory=list)
    suggested_targets: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# --- from previews.py


class SerpPreview(BaseModel):
    device: str
    display_url: str
    title: str
    title_shown: str
    title_truncated: bool
    title_px: int
    description: str
    description_shown: str
    description_truncated: bool
    description_missing: bool


class SocialImage(BaseModel):
    url: str | None = None
    absolute: bool = True
    status: int | None = None
    reachable: bool | None = None
    width: int | None = None
    height: int | None = None
    format: str | None = None
    bytes: int | None = None
    bytes_is_lower_bound: bool = False
    too_heavy: bool = False
    ratio: float | None = None
    error: str | None = None


class SocialCard(BaseModel):
    og: dict[str, str] = Field(default_factory=dict)
    twitter: dict[str, str] = Field(default_factory=dict)
    image: SocialImage = Field(default_factory=SocialImage)
    missing_og: list[str] = Field(default_factory=list)


class Previews(BaseModel):
    serp: list[SerpPreview] = Field(default_factory=list)
    social: SocialCard = Field(default_factory=SocialCard)
    notes: list[str] = Field(default_factory=list)


# --- from schema_suggest.py


class SchemaSuggestion(BaseModel):
    url: str
    type: str
    reason: str
    json_ld: str
    complete: bool
    missing_fields: list[str] = Field(default_factory=list)
    validation_issues: list[str] = Field(default_factory=list)  # our own schema rule ids
    rich_result_note: str = ""
    warning: str = ""


class SchemaSuggestions(BaseModel):
    items: list[SchemaSuggestion] = Field(default_factory=list)
    pages_examined: int = 0
    product_gaps: list[dict[str, Any]] = Field(default_factory=list)  # existing Product markup
    notes: list[str] = Field(default_factory=list)


# --- from local.py


class PhoneEntry(BaseModel):
    number: str  # normalised (+90..., +49..., digits when the country is unknown)
    formats: list[str] = Field(default_factory=list)  # how it is written (raw spellings)
    pages: list[str] = Field(default_factory=list)
    sources: list[Literal["schema", "tel-link", "text"]] = Field(default_factory=list)


class AddressEntry(BaseModel):
    text: str
    source: Literal["schema", "text"]  # "text" is a heuristic candidate
    pages: list[str] = Field(default_factory=list)


class NapInconsistency(BaseModel):
    kind: Literal["phone", "address"]
    code: Literal["schema-phone-not-on-page", "schema-phones-differ", "schema-addresses-differ"]
    values: list[str] = Field(default_factory=list)
    pages: list[str] = Field(default_factory=list)


class LocalSeo(BaseModel):
    applicable: bool = False
    signals: list[str] = Field(default_factory=list)  # stable codes the report translates
    business_types: list[str] = Field(default_factory=list)
    names: list[str] = Field(default_factory=list)
    phones: list[PhoneEntry] = Field(default_factory=list)
    addresses: list[AddressEntry] = Field(default_factory=list)
    inconsistencies: list[NapInconsistency] = Field(default_factory=list)
    has_tel_link: bool = False
    has_map_embed: bool = False
    pages_scanned: int = 0
    notes: list[str] = Field(default_factory=list)


# --- from schema.py


class SchemaReport(BaseModel):
    url: str
    checked: bool = False  # False when the page could not be parsed
    types: list[str] = Field(default_factory=list)
    blocks: int = 0  # JSON-LD blocks on the page
    facts: dict[str, Any] = Field(default_factory=dict)
