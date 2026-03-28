# recom_sys_app/services/collaborative.py
"""
CollaborativeFilteringService: user-based collaborative filtering
using cosine similarity on interaction vectors.
"""
from django.core.cache import cache
from django.contrib.auth import get_user_model
from django.db.models import Count
from collections import defaultdict
import math
import logging

from ..models import Interaction
from .tmdb_client import TmdbClient

_logger = logging.getLogger(__name__)
User = get_user_model()


class CollaborativeFilteringService:
    """
    Collaborative Filtering Service for movie recommendations.

    Uses user-based collaborative filtering to find similar users and recommend
    movies based on what similar users liked.

    Features:
    - User similarity calculation using cosine similarity
    - Cached similarity matrices for performance
    - Integration with preference-based recommendations
    - Hybrid recommendation approach
    """

    SIMILARITY_CACHE_TIMEOUT = 86400  # 24 hours
    RECOMMENDATION_CACHE_TIMEOUT = 3600  # 1 hour
    MIN_INTERACTIONS_FOR_CF = 3
    MIN_SIMILAR_USERS = 3
    SIMILARITY_THRESHOLD = 0.1

    @classmethod
    def get_user_interaction_vector(cls, user):
        """
        Build interaction vector for a user.

        Returns a dict mapping tmdb_id -> interaction score:
        - LIKE / WATCHED_LIKED: 2.0
        - DISLIKE / WATCHED_DISLIKED: -1.0
        - WATCH_LATER: 0.5
        - WATCHED (neutral): 0.0
        Rating (1-10) adds ±0.5 on top when present.
        """
        interactions = Interaction.objects.filter(user=user).select_related()

        vector = {}
        for interaction in interactions:
            tmdb_id = interaction.tmdb_id
            status = interaction.status

            if status in [Interaction.Status.LIKE, Interaction.Status.WATCHED_LIKED]:
                score = 2.0
            elif status in [Interaction.Status.DISLIKE, Interaction.Status.WATCHED_DISLIKED]:
                score = -1.0
            elif status == Interaction.Status.WATCH_LATER:
                score = 0.5
            else:
                score = 0.0

            if interaction.rating:
                normalized_rating = (interaction.rating - 5.5) / 4.5
                score = score + normalized_rating * 0.5

            vector[tmdb_id] = vector.get(tmdb_id, 0.0) + score

        return vector

    @classmethod
    def cosine_similarity(cls, vector1, vector2):
        """
        Calculate cosine similarity between two interaction vectors.

        Returns:
            float: Similarity score clamped to [0.0, 1.0]
        """
        common_movies = set(vector1.keys()) & set(vector2.keys())
        if not common_movies:
            return 0.0

        dot_product = sum(vector1[movie] * vector2[movie] for movie in common_movies)
        magnitude1 = math.sqrt(sum(score**2 for score in vector1.values()))
        magnitude2 = math.sqrt(sum(score**2 for score in vector2.values()))

        if magnitude1 == 0 or magnitude2 == 0:
            return 0.0

        return max(0.0, min(1.0, dot_product / (magnitude1 * magnitude2)))

    @classmethod
    def find_similar_users(cls, user, limit=20, min_similarity=None):
        """
        Find users similar to the given user based on interaction patterns.

        Returns:
            list of (similar_user, similarity_score) tuples sorted by score descending
        """
        if min_similarity is None:
            min_similarity = cls.SIMILARITY_THRESHOLD

        cache_key = f"similar_users_{user.id}"
        cached_similar = cache.get(cache_key)
        if cached_similar:
            return cached_similar[:limit]

        user_vector = cls.get_user_interaction_vector(user)
        if len(user_vector) < cls.MIN_INTERACTIONS_FOR_CF:
            return []

        other_users = (
            User.objects.exclude(id=user.id)
            .filter(interactions__isnull=False)
            .distinct()
        )

        similar_users = []
        for other_user in other_users:
            other_vector = cls.get_user_interaction_vector(other_user)
            if len(other_vector) < cls.MIN_INTERACTIONS_FOR_CF:
                continue

            similarity = cls.cosine_similarity(user_vector, other_vector)
            if similarity >= min_similarity:
                similar_users.append((other_user, similarity))

        similar_users.sort(key=lambda x: x[1], reverse=True)
        cache.set(cache_key, similar_users, cls.SIMILARITY_CACHE_TIMEOUT)
        return similar_users[:limit]

    @classmethod
    def get_collaborative_recommendations(cls, user, limit=50, min_similar_users=None):
        """
        Get movie recommendations using collaborative filtering.

        Strategy:
        1. Find similar users
        2. Score movies they liked (that current user hasn't seen) by weighted similarity
        3. Boost movies liked by multiple similar users via log-weighting
        4. Return top recommendations

        Returns:
            list of tmdb_id values sorted by score descending
        """
        if min_similar_users is None:
            min_similar_users = cls.MIN_SIMILAR_USERS

        cache_key = f"cf_recommendations_{user.id}"
        cached_recs = cache.get(cache_key)
        if cached_recs:
            return cached_recs[:limit]

        similar_users = cls.find_similar_users(user, limit=50)
        if len(similar_users) < min_similar_users:
            return []

        user_interactions = set(
            Interaction.objects.filter(user=user).values_list("tmdb_id", flat=True)
        )

        movie_scores = defaultdict(float)
        movie_counts = defaultdict(int)

        for similar_user, similarity_score in similar_users:
            liked_movies = Interaction.objects.filter(
                user=similar_user,
                status__in=[Interaction.Status.LIKE, Interaction.Status.WATCHED_LIKED],
            ).values_list("tmdb_id", flat=True)

            for tmdb_id in liked_movies:
                if tmdb_id not in user_interactions:
                    movie_scores[tmdb_id] += similarity_score
                    movie_counts[tmdb_id] += 1

        if not movie_scores:
            return []

        for tmdb_id in movie_scores:
            count = movie_counts[tmdb_id]
            movie_scores[tmdb_id] = movie_scores[tmdb_id] * (1 + math.log(count + 1))

        sorted_movies = sorted(movie_scores.items(), key=lambda x: x[1], reverse=True)
        recommendations = [tmdb_id for tmdb_id, _ in sorted_movies]

        cache.set(cache_key, recommendations, cls.RECOMMENDATION_CACHE_TIMEOUT)
        return recommendations[:limit]

    @classmethod
    def get_hybrid_recommendations(
        cls, user, limit=50, cf_weight=0.4, preference_weight=0.4, popular_weight=0.2
    ):
        """
        Combine collaborative filtering, preference-based, and popular movie
        recommendations with configurable weights.

        Returns:
            list of tmdb_id values sorted by combined score descending
        """
        total_weight = cf_weight + preference_weight + popular_weight
        if total_weight > 0:
            cf_weight /= total_weight
            preference_weight /= total_weight
            popular_weight /= total_weight

        cache_key = f"hybrid_recommendations_{user.id}_{cf_weight}_{preference_weight}"
        cached_recs = cache.get(cache_key)
        if cached_recs:
            return cached_recs[:limit]

        all_movies = {}

        # 1. Collaborative filtering
        cf_movies = cls.get_collaborative_recommendations(user, limit=limit * 2)
        for idx, tmdb_id in enumerate(cf_movies):
            score = (len(cf_movies) - idx) * cf_weight
            all_movies[tmdb_id] = all_movies.get(tmdb_id, 0.0) + score

        # 2. Preference-based
        try:
            from ..models import UserPreference
            from .recommendation import RecommendationService

            preference = UserPreference.objects.get(user=user)
            if preference.genre_preferences and preference.total_interactions > 0:
                pref_movies = RecommendationService._generate_solo_recommendations_from_preferences(
                    user, preference, limit * 2
                )
                for idx, tmdb_id in enumerate(pref_movies):
                    score = (len(pref_movies) - idx) * preference_weight
                    all_movies[tmdb_id] = all_movies.get(tmdb_id, 0.0) + score
        except Exception:
            pass

        # 3. Popular movies fallback
        if popular_weight > 0:
            popular_movies = TmdbClient._get_popular_movies(limit=limit)
            for idx, tmdb_id in enumerate(popular_movies):
                score = (len(popular_movies) - idx) * popular_weight * 0.5
                all_movies[tmdb_id] = all_movies.get(tmdb_id, 0.0) + score

        sorted_movies = sorted(all_movies.items(), key=lambda x: x[1], reverse=True)
        recommendations = [tmdb_id for tmdb_id, _ in sorted_movies]

        cache.set(cache_key, recommendations, cls.RECOMMENDATION_CACHE_TIMEOUT)
        return recommendations[:limit]

    @classmethod
    def invalidate_user_cache(cls, user):
        """Invalidate all cached data for a user (call when user interactions change)."""
        for key in [f"similar_users_{user.id}", f"cf_recommendations_{user.id}"]:
            cache.delete(key)

        for weight_cf in [0.3, 0.4, 0.5]:
            for weight_pref in [0.3, 0.4, 0.5]:
                cache.delete(
                    f"hybrid_recommendations_{user.id}_{weight_cf}_{weight_pref}"
                )

    @classmethod
    def precompute_similarities(cls, user_ids=None, batch_size=100):
        """
        Precompute user similarities for better performance.
        Useful for background jobs to warm up the cache.
        """
        if user_ids is None:
            user_ids = list(
                User.objects.annotate(interaction_count=Count("interactions"))
                .filter(interaction_count__gte=cls.MIN_INTERACTIONS_FOR_CF)
                .values_list("id", flat=True)
            )

        processed = 0
        for user_id in user_ids:
            try:
                user = User.objects.get(id=user_id)
                cls.find_similar_users(user, limit=20)
                processed += 1

                if processed % batch_size == 0:
                    print(f"Processed {processed} users...")
            except User.DoesNotExist:
                continue

        return processed
