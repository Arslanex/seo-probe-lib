"""The content feature: readability, previews, structured data and local SEO of a page."""

from seoprobe.core.feature import Feature

from .local_seo import local_seo
from .models import (
    AddressEntry,
    KeywordCheck,
    LocalSeo,
    NapInconsistency,
    PhoneEntry,
    Previews,
    ReadabilityData,
    SchemaReport,
    SchemaSuggestion,
    SchemaSuggestions,
    SerpPreview,
    SocialCard,
    SocialImage,
)
from .previews import previews
from .readability import readability, readability_scores, supported_language
from .schema import run_schema_checks, schema, suggest_schema

FEATURE = Feature(
    name="content",
    package=__name__,
    operations=[readability, previews, schema, suggest_schema, local_seo],
    depends_on=["page"],
)

__all__ = [
    "readability",
    "previews",
    "schema",
    "suggest_schema",
    "local_seo",
    "KeywordCheck",
    "ReadabilityData",
    "SerpPreview",
    "SocialImage",
    "SocialCard",
    "Previews",
    "SchemaReport",
    "SchemaSuggestion",
    "SchemaSuggestions",
    "PhoneEntry",
    "AddressEntry",
    "NapInconsistency",
    "LocalSeo",
    "readability_scores",
    "supported_language",
    "run_schema_checks",
]
