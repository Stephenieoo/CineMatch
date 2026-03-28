# recom_sys_app/services/recommendation.py
"""
Core recommendation business logic for solo and group movie swipe sessions.
TMDB API calls are delegated to TmdbClient.
"""
from django.core.cache import cache
from collections import Counter
import logging

from ..models import GroupMember, GroupSwipe, Interaction, UserProfile
from .tmdb_client import TmdbClient

_logger = logging.getLogger(__name__)


class RecommendationService:
    """群组 / 个人电影推荐服务"""

    CACHE_TIMEOUT = TmdbClient.CACHE_TIMEOUT

    # ---------------------------------------------------------------------------
    # Proxy attributes / methods (backward-compatibility with external callers
    # and tests that reference RecommendationService.X directly)
    # ---------------------------------------------------------------------------

    # TMDB_TOKEN is still exposed so tests can patch it via RecommendationService
    TMDB_TOKEN = TmdbClient.TMDB_TOKEN

    @classmethod
    def get_movie_details(cls, tmdb_id):
        return TmdbClient.get_movie_details(tmdb_id)

    @classmethod
    def search_movies(cls, query, limit=10):
        return TmdbClient.search_movies(query, limit)

    @classmethod
    def get_similar_movies(cls, tmdb_id, limit=20):
        return TmdbClient.get_similar_movies(tmdb_id, limit)

    @classmethod
    def _get_popular_movies(cls, limit=50, randomize=True):
        return TmdbClient._get_popular_movies(limit, randomize)

    @classmethod
    def _get_movies_by_genres(cls, genre_ids, limit=100, randomize=True):
        return TmdbClient._get_movies_by_genres(genre_ids, limit, randomize)

    @classmethod
    def _get_genre_ids_by_names(cls, genre_names):
        return TmdbClient._get_genre_ids_by_names(genre_names)

    # ---------------------------------------------------------------------------
    # Group deck
    # ---------------------------------------------------------------------------

    @classmethod
    def get_group_deck(
        cls,
        group_session,
        user=None,
        limit=50,
        selected_genre_ids=None,
        use_collaborative_filtering=False,
    ):
        """
        为群组生成个性化电影推荐列表

        Args:
            group_session: GroupSession 实例
            user: User 实例（可选，用于过滤该用户已滑过的电影）
            limit: 返回电影数量
            selected_genre_ids: Optional list of genre IDs to filter by (default: None)

        Returns:
            list: 电影 tmdb_id 列表
        """
        if user:
            cache_key = f"group_deck_{group_session.id}_user_{user.id}_cf_{use_collaborative_filtering}"
        else:
            cache_key = f"group_deck_{group_session.id}_cf_{use_collaborative_filtering}"

        cached_deck = cache.get(cache_key)
        if cached_deck:
            return cached_deck[:limit]

        members = GroupMember.objects.filter(
            group_session=group_session, is_active=True
        ).select_related("user")

        movie_ids = []

        if members.count() < 2:
            movie_ids = TmdbClient._get_popular_movies(limit * 2)
        else:
            movie_ids = cls._generate_group_recommendations(group_session, members, limit * 2)

        # Add collaborative filtering recommendations if enabled and user provided
        if use_collaborative_filtering and user:
            try:
                from .collaborative import CollaborativeFilteringService

                interaction_count = Interaction.objects.filter(user=user).count()
                if interaction_count >= CollaborativeFilteringService.MIN_INTERACTIONS_FOR_CF:
                    cf_movies = CollaborativeFilteringService.get_collaborative_recommendations(
                        user, limit=limit
                    )
                    existing_ids = set(movie_ids)
                    cf_filtered = [mid for mid in cf_movies if mid not in existing_ids]
                    movie_ids = cf_filtered[: limit // 2] + movie_ids
            except Exception as e:
                print(f"Collaborative filtering failed in group deck: {e}")

        # Filter swiped movies (per-user if user provided, else group-wide)
        if user:
            swiped_ids = set(
                GroupSwipe.objects.filter(
                    group_session=group_session, user=user
                ).values_list("tmdb_id", flat=True)
            )
        else:
            swiped_ids = set(
                GroupSwipe.objects.filter(group_session=group_session).values_list(
                    "tmdb_id", flat=True
                )
            )

        filtered_movies = [mid for mid in movie_ids if mid not in swiped_ids]

        # Filter by selected genres if provided
        if selected_genre_ids:
            genre_based_movies = TmdbClient._get_movies_by_genres(
                selected_genre_ids, limit=limit * 3, randomize=True
            )
            genre_based_movies = [mid for mid in genre_based_movies if mid not in swiped_ids]

            genre_filtered_group = []
            for tmdb_id in filtered_movies[: limit * 3]:
                if tmdb_id in swiped_ids:
                    continue
                try:
                    movie_details = TmdbClient.get_movie_details(tmdb_id)
                    if movie_details:
                        movie_genres = movie_details.get("genres", [])
                        movie_genre_ids = []
                        for genre in movie_genres:
                            if isinstance(genre, dict) and "id" in genre:
                                movie_genre_ids.append(genre.get("id"))
                            elif isinstance(genre, int):
                                movie_genre_ids.append(genre)

                        if any(gid in selected_genre_ids for gid in movie_genre_ids):
                            genre_filtered_group.append(tmdb_id)

                        if len(genre_filtered_group) >= limit * 2:
                            break
                except Exception:
                    continue

            combined_ids = list(dict.fromkeys(genre_based_movies + genre_filtered_group))
            filtered_movies = [mid for mid in combined_ids if mid not in swiped_ids]

        import random

        if len(filtered_movies) > limit:
            random.shuffle(filtered_movies)

        cache.set(cache_key, filtered_movies, cls.CACHE_TIMEOUT)
        return filtered_movies[:limit]

    # ---------------------------------------------------------------------------
    # Solo deck
    # ---------------------------------------------------------------------------

    @classmethod
    def get_solo_deck(
        cls,
        user,
        limit=50,
        use_collaborative_filtering=True,
        offset=0,
        selected_genre_ids=None,
    ):
        """
        Generate personalized movie recommendations for solo mode.
        Uses hybrid approach: collaborative filtering + preference-based recommendations.
        Supports pagination via offset for variety.
        """
        cache_key = f"solo_deck_{user.id}_{use_collaborative_filtering}"
        cached_deck = cache.get(cache_key)
        if cached_deck and offset == 0:
            return cached_deck[:limit]
        elif cached_deck and offset > 0:
            if offset < len(cached_deck):
                return cached_deck[offset : offset + limit]  # noqa: E203

        generation_limit = max(limit * 3, 150)
        movie_ids = []

        swiped_ids = set(
            Interaction.objects.filter(user=user).values_list("tmdb_id", flat=True)
        )

        preference_based_ids = set()
        if selected_genre_ids:
            min_movies_needed = max(limit, 50)
            fetch_limit = max(generation_limit * 2, min_movies_needed * 3)

            genre_based_movies = TmdbClient._get_movies_by_genres(
                selected_genre_ids, limit=fetch_limit, randomize=True
            )
            genre_filtered = [mid for mid in genre_based_movies if mid not in swiped_ids]

            pref_movies_list = []
            try:
                from ..models import UserPreference

                preference = UserPreference.objects.get(user=user)
                if preference.genre_preferences and preference.total_interactions > 0:
                    target_pref_count = max(int(limit * 0.25), 10)
                    pref_movies = cls._generate_solo_recommendations_from_preferences(
                        user, preference, limit=target_pref_count * 2
                    )
                    pref_filtered = []
                    for mid in pref_movies:
                        if mid not in swiped_ids and mid not in genre_filtered:
                            movie_details = TmdbClient.get_movie_details(mid)
                            if movie_details:
                                movie_genres = movie_details.get("genres", [])
                                movie_genre_ids = TmdbClient._get_genre_ids_by_names(movie_genres)
                                if any(gid in selected_genre_ids for gid in movie_genre_ids):
                                    pref_filtered.append(mid)
                                    if len(pref_filtered) >= target_pref_count:
                                        break
                    pref_movies_list = pref_filtered[:target_pref_count]
                    preference_based_ids = set(pref_movies_list)
            except UserPreference.DoesNotExist:
                pass

            # Interleave: 3 genre movies, then 1 preference movie
            filtered_movies = []
            pref_index = 0
            genre_index = 0
            pref_count = len(pref_movies_list)
            genre_count = len(genre_filtered)

            while len(filtered_movies) < limit and (
                pref_index < pref_count or genre_index < genre_count
            ):
                for _ in range(3):
                    if genre_index < genre_count:
                        filtered_movies.append(genre_filtered[genre_index])
                        genre_index += 1
                        if len(filtered_movies) >= limit:
                            break

                if pref_index < pref_count and len(filtered_movies) < limit:
                    filtered_movies.append(pref_movies_list[pref_index])
                    pref_index += 1

                if pref_index >= pref_count and genre_index < genre_count:
                    remaining = limit - len(filtered_movies)
                    if remaining > 0:
                        filtered_movies.extend(genre_filtered[genre_index : genre_index + remaining])
                    break
                elif genre_index >= genre_count and pref_index < pref_count:
                    remaining = limit - len(filtered_movies)
                    if remaining > 0:
                        filtered_movies.extend(pref_movies_list[pref_index : pref_index + remaining])
                    break

            filtered_movies = filtered_movies[:limit]

            if preference_based_ids:
                cache.set(
                    f"solo_pref_ids_{user.id}_{'_'.join(map(str, selected_genre_ids))}",
                    preference_based_ids,
                    cls.CACHE_TIMEOUT,
                )
        else:
            if not movie_ids or len(movie_ids) < limit:
                popular_movies = TmdbClient._get_popular_movies(limit * 2)
                movie_ids = list(dict.fromkeys(list(movie_ids) + popular_movies))

            filtered_movies = [mid for mid in movie_ids if mid not in swiped_ids]

        import random

        if len(filtered_movies) > limit and preference_based_ids:
            pref_count = len([m for m in filtered_movies if m in preference_based_ids])
            if pref_count > 0:
                genre_portion = filtered_movies[pref_count:]
                random.shuffle(genre_portion)
                filtered_movies = filtered_movies[:pref_count] + genre_portion
            else:
                random.shuffle(filtered_movies)
        elif len(filtered_movies) > limit:
            random.shuffle(filtered_movies)

        if cached_deck and offset > 0:
            existing_ids = set(cached_deck)
            new_movies = [mid for mid in filtered_movies if mid not in existing_ids]
            filtered_movies = cached_deck + new_movies
            cache.set(cache_key, filtered_movies, cls.CACHE_TIMEOUT)
            if offset < len(filtered_movies):
                return filtered_movies[offset : offset + limit]  # noqa: E203
            else:
                return []
        else:
            cache.set(cache_key, filtered_movies, cls.CACHE_TIMEOUT)
            return filtered_movies[: max(limit, min(50, len(filtered_movies)))]

    # ---------------------------------------------------------------------------
    # Internal recommendation generators
    # ---------------------------------------------------------------------------

    @classmethod
    def _generate_solo_recommendations_from_preferences(cls, user, preference, limit=100):
        """Generate recommendations using UserPreference genre scores."""
        top_genres = preference.get_top_genres(limit=5)
        if not top_genres:
            return cls._generate_solo_recommendations_from_history_or_profile(user, limit)

        genre_names = [genre for genre, _ in top_genres]
        genre_scores = {genre: score for genre, score in top_genres}

        genre_ids = TmdbClient._get_genre_ids_by_names(genre_names)
        if not genre_ids:
            return cls._generate_solo_recommendations_from_history_or_profile(user, limit)

        all_movies = []
        for genre_id in genre_ids:
            movies = TmdbClient._get_movies_by_genres([genre_id], limit // len(genre_ids) + 10)
            all_movies.extend(movies)

        scored_movies = []
        for tmdb_id in all_movies:
            movie_details = TmdbClient.get_movie_details(tmdb_id)
            if not movie_details:
                continue

            movie_genres = movie_details.get("genres", [])
            score = sum(genre_scores.get(g, 0.0) for g in movie_genres)
            if movie_genres:
                score = score / len(movie_genres)

            scored_movies.append((tmdb_id, score))

        scored_movies.sort(key=lambda x: x[1], reverse=True)
        return [tmdb_id for tmdb_id, _ in scored_movies[:limit]]

    @classmethod
    def _generate_solo_recommendations_from_history_or_profile(cls, user, limit=100):
        """Fallback: use history or profile-based recommendations."""
        liked_interactions = Interaction.objects.filter(
            user=user, status=Interaction.Status.LIKE
        ).values_list("tmdb_id", flat=True)

        if liked_interactions.count() > 0:
            return cls._generate_solo_recommendations_from_history(
                user, list(liked_interactions), limit
            )
        else:
            return cls._generate_solo_recommendations_from_profile(user, limit)

    @classmethod
    def _generate_solo_recommendations_from_history(cls, user, liked_movie_ids, limit=100):
        """Generate recommendations based on user's like history."""
        if not liked_movie_ids:
            return TmdbClient._get_popular_movies(limit)

        all_genres = []
        for tmdb_id in liked_movie_ids[:10]:
            movie_details = TmdbClient.get_movie_details(tmdb_id)
            if movie_details and movie_details.get("genres"):
                all_genres.extend(movie_details["genres"])

        if not all_genres:
            return TmdbClient._get_popular_movies(limit)

        genre_counter = Counter(all_genres)
        top_genres = [genre for genre, _ in genre_counter.most_common(3)]
        genre_ids = TmdbClient._get_genre_ids_by_names(top_genres)

        if genre_ids:
            return TmdbClient._get_movies_by_genres(genre_ids, limit)
        return TmdbClient._get_popular_movies(limit)

    @classmethod
    def _generate_solo_recommendations_from_profile(cls, user, limit=100):
        """Generate recommendations based on user's onboarding preferences."""
        try:
            profile = UserProfile.objects.get(user=user)
        except UserProfile.DoesNotExist:
            return TmdbClient._get_popular_movies(limit)

        favorite_genres = []
        if profile.favourite_genre1:
            favorite_genres.append(profile.favourite_genre1)
        if profile.favourite_genre2:
            favorite_genres.append(profile.favourite_genre2)

        if not favorite_genres:
            return TmdbClient._get_popular_movies(limit)

        genre_ids = TmdbClient._get_genre_ids_by_names(favorite_genres)
        if genre_ids:
            return TmdbClient._get_movies_by_genres(genre_ids, limit)
        return TmdbClient._get_popular_movies(limit)

    @classmethod
    def _generate_group_recommendations(cls, group_session, members, limit=100):
        """基于群组历史 likes 生成推荐"""
        liked_movie_ids = list(
            GroupSwipe.objects.filter(
                group_session=group_session, action=GroupSwipe.Action.LIKE
            )
            .values_list("tmdb_id", flat=True)
            .distinct()
        )

        if not liked_movie_ids:
            return TmdbClient._get_popular_movies(limit)

        all_genres = []
        for tmdb_id in liked_movie_ids[:10]:
            movie_details = TmdbClient.get_movie_details(tmdb_id)
            if movie_details and movie_details.get("genres"):
                all_genres.extend(movie_details["genres"])

        if not all_genres:
            return TmdbClient._get_popular_movies(limit)

        genre_counter = Counter(all_genres)
        top_genres = [genre for genre, _ in genre_counter.most_common(3)]
        genre_ids = TmdbClient._get_genre_ids_by_names(top_genres)

        if genre_ids:
            return TmdbClient._get_movies_by_genres(genre_ids, limit)
        return TmdbClient._get_popular_movies(limit)

    # ---------------------------------------------------------------------------
    # Group management helpers
    # ---------------------------------------------------------------------------

    @classmethod
    def check_group_match(cls, group_session, tmdb_id):
        """检查是否所有活跃成员都喜欢这部电影"""
        active_member_count = GroupMember.objects.filter(
            group_session=group_session, is_active=True
        ).count()

        like_count = GroupSwipe.objects.filter(
            group_session=group_session, tmdb_id=tmdb_id, action=GroupSwipe.Action.LIKE
        ).count()

        print(
            f"[DEBUG check_group_match] active_members: {active_member_count}, likes: {like_count}, tmdb_id: {tmdb_id}"
        )

        is_match = like_count >= active_member_count and active_member_count > 0
        print(f"[DEBUG check_group_match] Result: {is_match}")
        return is_match

    @classmethod
    def invalidate_deck_cache(cls, group_session):
        """清除群组推荐缓存（当有新的 swipe 或成员变化时调用）"""
        cache.delete(f"group_deck_{group_session.id}")

        active_members = GroupMember.objects.filter(
            group_session=group_session, is_active=True
        ).select_related("user")

        for member in active_members:
            for use_cf in [True, False]:
                cache.delete(
                    f"group_deck_{group_session.id}_user_{member.user.id}_cf_{use_cf}"
                )
            cache.delete(f"group_deck_{group_session.id}_user_{member.user.id}")
            print(f"[DEBUG] Cleared cache for user {member.user.username} (all variants)")

    @classmethod
    def check_all_members_finished(cls, group_session):
        """检查是否所有成员都滑完了"""
        active_members = GroupMember.objects.filter(
            group_session=group_session, is_active=True
        ).select_related("user")

        total_members = active_members.count()

        print(f"[DEBUG check_finished] Group: {group_session.group_code}")
        print(f"[DEBUG check_finished] Total active members: {total_members}")

        if total_members == 0:
            return {
                "all_finished": False,
                "total_members": 0,
                "finished_members": 0,
                "total_movies": 20,
            }

        MOVIES_PER_ROUND = 20
        total_movies = MOVIES_PER_ROUND
        print(f"[DEBUG check_finished] Movies per round: {total_movies}")

        finished_members = 0
        for member in active_members:
            swipe_count = GroupSwipe.objects.filter(
                group_session=group_session, user=member.user
            ).count()

            print(f"[DEBUG check_finished] User: {member.user.username} (ID: {member.user.id})")
            print(f"[DEBUG check_finished]   - Total swipes: {swipe_count}")

            if swipe_count >= MOVIES_PER_ROUND:
                print("[DEBUG check_finished]   - ✅ User FINISHED!")
                finished_members += 1
            else:
                print(
                    f"[DEBUG check_finished]   - ❌ NOT finished ({swipe_count}/{MOVIES_PER_ROUND})"
                )

        all_finished = (finished_members == total_members) and total_members > 0

        print(f"[DEBUG check_finished] Result: {finished_members}/{total_members} finished")
        print(f"[DEBUG check_finished] All finished: {all_finished}")
        print(
            f"[DEBUG check_finished] Active members list: {[m.user.username for m in active_members]}"
        )

        return {
            "all_finished": all_finished,
            "total_members": total_members,
            "finished_members": finished_members,
            "total_movies": total_movies,
        }

    @classmethod
    def get_all_common_matches(cls, group_session):
        """获取所有成员都喜欢的电影列表"""
        from django.db.models import Count

        print(f"[DEBUG get_all_common_matches] Group: {group_session.group_code}")

        active_members = GroupMember.objects.filter(
            group_session=group_session, is_active=True
        )
        total_members = active_members.count()
        print(f"[DEBUG get_all_common_matches] Total active members: {total_members}")

        if total_members == 0:
            return []

        common_movie_ids = list(
            GroupSwipe.objects.filter(
                group_session=group_session, action=GroupSwipe.Action.LIKE
            )
            .values("tmdb_id")
            .annotate(like_count=Count("id"))
            .filter(like_count=total_members)
            .values_list("tmdb_id", flat=True)
        )

        print(f"[DEBUG get_all_common_matches] Found {len(common_movie_ids)} common matches")
        print(f"[DEBUG get_all_common_matches] Movie IDs: {common_movie_ids}")

        result = []
        for tmdb_id in common_movie_ids:
            movie_info = TmdbClient.get_movie_details(tmdb_id)
            if not movie_info:
                continue

            poster_url = None
            if movie_info.get("poster_path"):
                poster_url = f"https://image.tmdb.org/t/p/w500{movie_info['poster_path']}"

            genres_list = []
            if movie_info.get("genres"):
                genres = movie_info["genres"]
                if isinstance(genres, list) and len(genres) > 0:
                    if isinstance(genres[0], dict):
                        genres_list = [g.get("name", str(g)) for g in genres]
                    elif isinstance(genres[0], str):
                        genres_list = genres

            movie_title = movie_info.get("title", f"Movie {tmdb_id}")
            result.append(
                {
                    "tmdb_id": tmdb_id,
                    "movie_title": movie_title,
                    "movie_info": movie_info,
                    "poster_url": poster_url,
                    "year": (
                        movie_info.get("release_date", "")[:4]
                        if movie_info.get("release_date")
                        else None
                    ),
                    "genres": genres_list,
                    "overview": movie_info.get("overview", ""),
                    "vote_average": movie_info.get("vote_average"),
                }
            )
            print(f"[DEBUG get_all_common_matches] Added movie: {movie_title}")

        print(f"[DEBUG get_all_common_matches] Returning {len(result)} movies")
        return result

    @classmethod
    def clear_group_swipes(cls, group_session):
        """清空群组的所有滑动记录，开始新一轮"""
        deleted_count = GroupSwipe.objects.filter(group_session=group_session).delete()[0]
        print(
            f"[DEBUG clear_swipes] Cleared {deleted_count} swipe records for group {group_session.group_code}"
        )

        deleted_count, _ = GroupSwipe.objects.filter(group_session=group_session).delete()
        print(f"[DEBUG clear_group_swipes] Deleted {deleted_count} swipe records")

        cls.invalidate_deck_cache(group_session)
        return deleted_count
