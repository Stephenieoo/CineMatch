# recom_sys_app/services/preference.py
"""
PreferenceService: calculate and persist user genre preferences
from interaction history to improve personalized recommendations.
"""
import logging

_logger = logging.getLogger(__name__)


class PreferenceService:
    """
    Service for calculating and updating user preferences based on interaction history.
    Automatically learns from user likes/dislikes to improve recommendations.
    """

    @classmethod
    def update_user_preferences(cls, user, force_recalculate=False):
        """
        Calculate and update user preferences based on interaction history.

        Args:
            user: User instance
            force_recalculate: If True, recalculate even if recently updated

        Returns:
            UserPreference: The updated preference object
        """
        from ..models import UserPreference, Interaction
        from django.db import transaction
        from django.utils import timezone
        from datetime import timedelta
        from .tmdb_client import TmdbClient

        preference, created = UserPreference.objects.get_or_create(user=user)

        if not force_recalculate and not created:
            recent_threshold = timezone.now() - timedelta(minutes=5)
            if preference.last_updated > recent_threshold:
                return preference

        interactions = Interaction.objects.filter(user=user).select_related()

        total_interactions = interactions.count()
        total_likes = interactions.filter(
            status__in=[
                Interaction.Status.LIKE,
                Interaction.Status.WATCHED_LIKED,
            ]
        ).count()
        total_dislikes = interactions.filter(
            status__in=[
                Interaction.Status.DISLIKE,
                Interaction.Status.WATCHED_DISLIKED,
            ]
        ).count()

        ratings = interactions.exclude(rating__isnull=True).values_list("rating", flat=True)
        average_rating = sum(ratings) / len(ratings) if ratings else None

        genre_weights = {}
        liked_movies = interactions.filter(
            status__in=[Interaction.Status.LIKE, Interaction.Status.WATCHED_LIKED]
        ).values_list("tmdb_id", flat=True)
        disliked_movies = interactions.filter(
            status__in=[Interaction.Status.DISLIKE, Interaction.Status.WATCHED_DISLIKED]
        ).values_list("tmdb_id", flat=True)

        # Process liked movies: +2 weight per genre
        for tmdb_id in liked_movies[:50]:
            movie_details = TmdbClient.get_movie_details(tmdb_id)
            if movie_details and movie_details.get("genres"):
                for genre_name in movie_details["genres"]:
                    genre_weights[genre_name] = genre_weights.get(genre_name, 0) + 2

        # Process disliked movies: -1 weight per genre
        for tmdb_id in disliked_movies[:20]:
            movie_details = TmdbClient.get_movie_details(tmdb_id)
            if movie_details and movie_details.get("genres"):
                for genre_name in movie_details["genres"]:
                    genre_weights[genre_name] = genre_weights.get(genre_name, 0) - 1

        # Normalize genre scores to 0.0-1.0
        genre_preferences = {}
        if genre_weights:
            min_weight = min(genre_weights.values())
            max_weight = max(genre_weights.values())
            weight_range = max_weight - min_weight if max_weight != min_weight else 1

            for genre, weight in genre_weights.items():
                normalized = (weight - min_weight) / weight_range if weight_range > 0 else 0.5
                genre_preferences[genre] = max(0.0, min(1.0, normalized))

        preferred_actors = []
        preferred_directors = []

        # Optional: Fetch credits for top liked movies (can be expensive)
        # Uncomment if you want to track actors/directors
        # for tmdb_id in liked_movies[:10]:  # Limit to avoid API rate limits
        #     try:
        #         credits_url = f"{TmdbClient.TMDB_BASE_URL}/movie/{tmdb_id}/credits"
        #         response = requests.get(
        #             credits_url,
        #             headers=TmdbClient.TMDB_HEADERS,
        #             timeout=10,
        #         )
        #         if response.status_code == 200:
        #             credits = response.json()
        #             cast = credits.get("cast", [])[:3]
        #             for actor in cast:
        #                 actor_id = actor.get("id")
        #                 if actor_id and actor_id not in preferred_actors:
        #                     preferred_actors.append(actor_id)
        #             crew = credits.get("crew", [])
        #             for person in crew:
        #                 if person.get("job") == "Director":
        #                     director_id = person.get("id")
        #                     if director_id and director_id not in preferred_directors:
        #                         preferred_directors.append(director_id)
        #     except Exception:
        #         pass

        with transaction.atomic():
            preference.genre_preferences = genre_preferences
            preference.preferred_actors = preferred_actors[:20]
            preference.preferred_directors = preferred_directors[:10]
            preference.average_rating_given = average_rating
            preference.total_interactions = total_interactions
            preference.total_likes = total_likes
            preference.total_dislikes = total_dislikes
            preference.save()

        return preference

    @classmethod
    def get_user_genre_scores(cls, user):
        """
        Get genre preference scores for a user.

        Args:
            user: User instance

        Returns:
            dict: Genre name -> score (0.0-1.0)
        """
        from ..models import UserPreference

        try:
            preference = UserPreference.objects.get(user=user)
            return preference.genre_preferences or {}
        except UserPreference.DoesNotExist:
            return {}
