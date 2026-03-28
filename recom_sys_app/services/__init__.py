# recom_sys_app/services/__init__.py
"""
Services package for recom_sys_app.

Re-exports the three service classes so that all existing import paths
(e.g. `from .services import RecommendationService`) continue to work
without any changes in views, signals, tests, or management commands.

Internal structure:
  tmdb_client.py   — TmdbClient: all TMDB HTTP calls + two-level cache
  recommendation.py — RecommendationService: group & solo recommendation logic
  preference.py    — PreferenceService: user genre preference learning
  collaborative.py — CollaborativeFilteringService: user-based CF
"""

from .recommendation import RecommendationService
from .preference import PreferenceService
from .collaborative import CollaborativeFilteringService

__all__ = [
    "RecommendationService",
    "PreferenceService",
    "CollaborativeFilteringService",
]
