"""
Off-site signals: domain authority (OpenPageRank), public visibility lookups and a light
competitor comparison.
"""

from seoprobe.core.feature import Feature

from .authority import authority, check_openpagerank_key
from .competitors import competitors
from .models import AuthoritySignal, CompetitorComparison, CompetitorRow, SkippedCompetitor, VisibilitySignals
from .visibility import visibility

FEATURE = Feature(
    name="offsite",
    package=__name__,
    operations=[authority, visibility, competitors],
    depends_on=["page", "content", "site"],
    access_checks={"openpagerank": check_openpagerank_key},
)

__all__ = [
    "authority",
    "visibility",
    "competitors",
    "VisibilitySignals",
    "AuthoritySignal",
    "CompetitorRow",
    "SkippedCompetitor",
    "CompetitorComparison",
]
