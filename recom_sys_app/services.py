# recom_sys_app/services.py
from django.core.cache import cache
from django.db.models import Count
from django.contrib.auth import get_user_model

# Count imported locally where needed
from collections import Counter, defaultdict
import requests
import os
import math
from .models import GroupMember, GroupSwipe, Interaction, UserProfile

User = get_user_model()


class RecommendationService:
    """群组电影推荐服务"""

    TMDB_TOKEN = os.getenv("TMDB_TOKEN") or os.getenv("TMDB_API_KEY")
    TMDB_BASE_URL = "https://api.themoviedb.org/3"
    TMDB_HEADERS = (
        {
            "Authorization": f"Bearer {TMDB_TOKEN}",
            "Accept": "application/json",
        }
        if TMDB_TOKEN
        else {}
    )
    CACHE_TIMEOUT = 3600  # 1小时缓存

    @classmethod
    def get_group_deck(cls, group_session, limit=50):
        """
        为群组生成个性化电影推荐列表

        For COMMUNITY groups: filter movies by community genre
        For PRIVATE groups: generate recommendations based on group member history

        Args:
            group_session: GroupSession 实例
            limit: 返回电影数量

        Returns:
            list: 电影 tmdb_id 列表
        """
        # 检查缓存
        cache_key = f"group_deck_{group_session.id}"
        cached_deck = cache.get(cache_key)
        if cached_deck:
            return cached_deck[:limit]

        # For COMMUNITY groups, filter by genre only
        if group_session.kind == "COMMUNITY":
            # Get genre from community_key or genre_filter
            genre_name = group_session.genre_filter or ""
            if not genre_name and group_session.community_key:
                # Extract from community_key (format: "genre:Action")
                if group_session.community_key.startswith("genre:"):
                    genre_name = group_session.community_key.split(":", 1)[1]

            print(f"[DEBUG get_group_deck] COMMUNITY mode - genre_name: {genre_name}")
            print(
                f"[DEBUG get_group_deck] community_key: {group_session.community_key}, genre_filter: {group_session.genre_filter}"
            )

            if genre_name:
                # Get genre IDs and fetch movies
                genre_ids = cls._get_genre_ids_by_names([genre_name])
                print(f"[DEBUG get_group_deck] genre_ids: {genre_ids}")
                if genre_ids:
                    movie_ids = cls._get_movies_by_genres(genre_ids, limit * 2)
                    print(
                        f"[DEBUG get_group_deck] Fetched {len(movie_ids)} movies for genre {genre_name}"
                    )
                else:
                    movie_ids = cls._get_popular_movies(limit * 2)
                    print(
                        "[DEBUG get_group_deck] No genre IDs found, using popular movies"
                    )
            else:
                movie_ids = cls._get_popular_movies(limit * 2)
                print(
                    "[DEBUG get_group_deck] No genre name found, using popular movies"
                )

            # For communities, filter out movies user already swiped via Interaction model
            from .models import Interaction

            # Get all users in community
            user_ids = GroupMember.objects.filter(
                group_session=group_session, is_active=True
            ).values_list("user_id", flat=True)

            # Get all swiped movie IDs by community members
            swiped_ids = set(
                Interaction.objects.filter(user_id__in=user_ids).values_list(
                    "tmdb_id", flat=True
                )
            )
        else:
            # For PRIVATE groups, use original logic
            # 获取活跃成员
            members = GroupMember.objects.filter(
                group_session=group_session, is_active=True
            ).select_related("user")

            if members.count() < 2:
                # 人数不足，返回热门电影
                movie_ids = cls._get_popular_movies(limit * 2)
            else:
                # 基于群组历史 likes 生成推荐（传递 group_session）
                movie_ids = cls._generate_group_recommendations(
                    group_session, members, limit * 2
                )

            # 过滤已经滑过的电影
            swiped_ids = set(
                GroupSwipe.objects.filter(group_session=group_session).values_list(
                    "tmdb_id", flat=True
                )
            )

        # 移除已滑过的电影
        filtered_movies = [mid for mid in movie_ids if mid not in swiped_ids]

        # 缓存结果
        cache.set(cache_key, filtered_movies, cls.CACHE_TIMEOUT)

        return filtered_movies[:limit]

    @classmethod
    def get_solo_deck(cls, user, limit=50, use_collaborative_filtering=True):
        """
        Generate personalized movie recommendations for solo mode.
        Uses hybrid approach: collaborative filtering + preference-based recommendations.

        Args:
            user: User instance
            limit: Number of movies to return
            use_collaborative_filtering: Whether to use collaborative filtering (default: True)

        Returns:
            list: Movie tmdb_id list
        """
        # Check cache
        cache_key = f"solo_deck_{user.id}_{use_collaborative_filtering}"
        cached_deck = cache.get(cache_key)
        if cached_deck:
            return cached_deck[:limit]

        # Get user's interaction count to determine best approach
        interaction_count = Interaction.objects.filter(user=user).count()

        # Use hybrid approach if CF is enabled and user has enough interactions
        if (
            use_collaborative_filtering
            and interaction_count
            >= CollaborativeFilteringService.MIN_INTERACTIONS_FOR_CF
        ):
            try:
                # Try hybrid recommendations (collaborative + preference-based)
                movie_ids = CollaborativeFilteringService.get_hybrid_recommendations(
                    user,
                    limit=limit * 2,
                    cf_weight=0.4,  # 40% collaborative filtering
                    preference_weight=0.4,  # 40% preference-based
                    popular_weight=0.2,  # 20% popular movies fallback
                )

                # If hybrid didn't return enough, supplement with preference-based
                if len(movie_ids) < limit:
                    from .models import UserPreference

                    try:
                        preference = UserPreference.objects.get(user=user)
                        if (
                            preference.genre_preferences
                            and preference.total_interactions > 0
                        ):
                            pref_movies = (
                                cls._generate_solo_recommendations_from_preferences(
                                    user, preference, limit * 2
                                )
                            )
                            # Add unique movies from preference-based
                            existing_ids = set(movie_ids)
                            for tmdb_id in pref_movies:
                                if tmdb_id not in existing_ids:
                                    movie_ids.append(tmdb_id)
                    except UserPreference.DoesNotExist:
                        pass

            except Exception as e:
                # Fallback to preference-based if CF fails
                print(
                    f"Collaborative filtering failed: {e}, falling back to preference-based"
                )
                movie_ids = cls._generate_solo_recommendations_from_history_or_profile(
                    user, limit * 2
                )
        else:
            # Use preference-based recommendations (original approach)
            from .models import UserPreference

            try:
                preference = UserPreference.objects.get(user=user)
                if preference.genre_preferences and preference.total_interactions > 0:
                    # Use preference-based recommendations
                    movie_ids = cls._generate_solo_recommendations_from_preferences(
                        user, preference, limit * 2
                    )
                else:
                    # Fallback to history-based
                    movie_ids = (
                        cls._generate_solo_recommendations_from_history_or_profile(
                            user, limit * 2
                        )
                    )
            except UserPreference.DoesNotExist:
                # No preferences yet, use history/profile
                movie_ids = cls._generate_solo_recommendations_from_history_or_profile(
                    user, limit * 2
                )

        # Filter out already-swiped movies
        swiped_ids = set(
            Interaction.objects.filter(user=user).values_list("tmdb_id", flat=True)
        )

        # Remove already-swiped movies
        filtered_movies = [mid for mid in movie_ids if mid not in swiped_ids]

        # Cache results
        cache.set(cache_key, filtered_movies, cls.CACHE_TIMEOUT)

        return filtered_movies[:limit]

    @classmethod
    def _generate_solo_recommendations_from_preferences(
        cls, user, preference, limit=100
    ):
        """
        Generate recommendations using UserPreference genre scores.

        Args:
            user: User instance
            preference: UserPreference instance
            limit: Number of movies to fetch

        Returns:
            list: Movie tmdb_id list
        """
        # Get top genres from preferences
        top_genres = preference.get_top_genres(limit=5)
        if not top_genres:
            return cls._generate_solo_recommendations_from_history_or_profile(
                user, limit
            )

        # Get genre names and scores
        genre_names = [genre for genre, _ in top_genres]
        genre_scores = {genre: score for genre, score in top_genres}

        # Get movies for top genres
        genre_ids = cls._get_genre_ids_by_names(genre_names)
        if not genre_ids:
            return cls._generate_solo_recommendations_from_history_or_profile(
                user, limit
            )

        # Fetch movies and score them by genre preference
        all_movies = []
        for genre_id in genre_ids:
            movies = cls._get_movies_by_genres([genre_id], limit // len(genre_ids) + 10)
            all_movies.extend(movies)

        # Score movies based on genre preferences
        scored_movies = []
        for tmdb_id in all_movies:
            movie_details = cls.get_movie_details(tmdb_id)
            if not movie_details:
                continue

            # Calculate weighted score based on genre preferences
            movie_genres = movie_details.get("genres", [])
            score = 0.0
            for genre_name in movie_genres:
                score += genre_scores.get(genre_name, 0.0)

            # Average score across genres
            if movie_genres:
                score = score / len(movie_genres)

            scored_movies.append((tmdb_id, score))

        # Sort by score and return top movies
        scored_movies.sort(key=lambda x: x[1], reverse=True)
        return [tmdb_id for tmdb_id, _ in scored_movies[:limit]]

    @classmethod
    def _generate_solo_recommendations_from_history_or_profile(cls, user, limit=100):
        """
        Fallback method: use history or profile-based recommendations.

        Args:
            user: User instance
            limit: Number of movies to return

        Returns:
            list: Movie tmdb_id list
        """
        # Get user's interaction history
        liked_interactions = Interaction.objects.filter(
            user=user, status=Interaction.Status.LIKE
        ).values_list("tmdb_id", flat=True)

        has_history = liked_interactions.count() > 0

        if has_history:
            # Returning user: use swipe history
            return cls._generate_solo_recommendations_from_history(
                user, list(liked_interactions), limit
            )
        else:
            # New user: use onboarding preferences
            return cls._generate_solo_recommendations_from_profile(user, limit)

    @classmethod
    def _generate_solo_recommendations_from_history(
        cls, user, liked_movie_ids, limit=100
    ):
        """
        Generate recommendations based on user's like history

        Strategy:
        1. Analyze genres from liked movies
        2. Recommend similar movies from those genres
        """
        if not liked_movie_ids:
            return cls._get_popular_movies(limit)

        # Extract genres from liked movies
        all_genres = []
        for tmdb_id in liked_movie_ids[:10]:  # Analyze up to 10 recent likes
            movie_details = cls.get_movie_details(tmdb_id)
            if movie_details and movie_details.get("genres"):
                all_genres.extend(movie_details["genres"])

        if not all_genres:
            return cls._get_popular_movies(limit)

        # Count genre frequency
        genre_counter = Counter(all_genres)

        # Get top 3 genres
        top_genres = [genre for genre, _ in genre_counter.most_common(3)]

        # Fetch movies from TMDB by those genres
        genre_ids = cls._get_genre_ids_by_names(top_genres)

        if genre_ids:
            movie_ids = cls._get_movies_by_genres(genre_ids, limit)
        else:
            movie_ids = cls._get_popular_movies(limit)

        return movie_ids

    @classmethod
    def _generate_solo_recommendations_from_profile(cls, user, limit=100):
        """
        Generate recommendations based on user's onboarding preferences

        Strategy:
        1. Use favourite_genre1 and favourite_genre2 from UserProfile
        2. Fetch popular movies from those genres
        """
        try:
            profile = UserProfile.objects.get(user=user)
        except UserProfile.DoesNotExist:
            return cls._get_popular_movies(limit)

        # Get user's favorite genres from profile
        favorite_genres = []
        if profile.favourite_genre1:
            favorite_genres.append(profile.favourite_genre1)
        if profile.favourite_genre2:
            favorite_genres.append(profile.favourite_genre2)

        if not favorite_genres:
            return cls._get_popular_movies(limit)

        # Convert genre names to IDs
        genre_ids = cls._get_genre_ids_by_names(favorite_genres)

        if genre_ids:
            movie_ids = cls._get_movies_by_genres(genre_ids, limit)
        else:
            movie_ids = cls._get_popular_movies(limit)

        return movie_ids

    @classmethod
    def _generate_group_recommendations(cls, group_session, members, limit=100):
        """
        基于群组历史 likes 生成推荐

        Args:
            group_session: GroupSession 实例
            members: GroupMember QuerySet
            limit: 返回电影数量

        策略：
        1. 找出群组成员都喜欢过的电影类型
        2. 基于这些类型推荐新电影
        """
        # 获取群组所有成员喜欢过的电影
        liked_movie_ids = list(
            GroupSwipe.objects.filter(
                group_session=group_session, action=GroupSwipe.Action.LIKE
            )
            .values_list("tmdb_id", flat=True)
            .distinct()
        )

        if not liked_movie_ids:
            # 没有历史数据，返回热门电影
            return cls._get_popular_movies(limit)

        # 从喜欢的电影中提取类型
        all_genres = []
        for tmdb_id in liked_movie_ids[:10]:  # 只分析最近10部
            movie_details = cls.get_movie_details(tmdb_id)
            if movie_details and movie_details.get("genres"):
                # genres 是字符串列表，如 ['Action', 'Thriller']
                all_genres.extend(movie_details["genres"])

        if not all_genres:
            return cls._get_popular_movies(limit)

        # 统计类型频率
        genre_counter = Counter(all_genres)

        # 选择最常见的3个类型
        top_genres = [genre for genre, _ in genre_counter.most_common(3)]

        # 从 TMDB 获取这些类型的电影
        # 注意：需要先将类型名转换为 genre_id
        genre_ids = cls._get_genre_ids_by_names(top_genres)

        if genre_ids:
            movie_ids = cls._get_movies_by_genres(genre_ids, limit)
        else:
            movie_ids = cls._get_popular_movies(limit)

        return movie_ids

    @classmethod
    def _get_genre_ids_by_names(cls, genre_names):
        """
        将类型名称转换为 TMDB genre_id

        TMDB 类型映射（常见的）:
        """
        genre_map = {
            "Action": 28,
            "Adventure": 12,
            "Animation": 16,
            "Comedy": 35,
            "Crime": 80,
            "Documentary": 99,
            "Drama": 18,
            "Family": 10751,
            "Fantasy": 14,
            "History": 36,
            "Horror": 27,
            "Music": 10402,
            "Mystery": 9648,
            "Romance": 10749,
            "Science Fiction": 878,
            "Thriller": 53,
            "War": 10752,
            "Western": 37,
        }

        genre_ids = []
        for name in genre_names:
            if name in genre_map:
                genre_ids.append(genre_map[name])

        return genre_ids

    @classmethod
    def _get_movies_by_genres(cls, genre_ids, limit=100):
        """
        从 TMDB 获取指定类型的高评分电影
        """
        try:
            # 构建类型筛选参数
            genre_str = "|".join(map(str, genre_ids))

            params = {
                "with_genres": genre_str,
                "sort_by": "vote_average.desc",
                "vote_count.gte": 100,  # 至少100个投票
                "page": 1,
            }

            response = requests.get(
                f"{cls.TMDB_BASE_URL}/discover/movie",
                params=params,
                headers=cls.TMDB_HEADERS,
                timeout=10,
            )
            response.raise_for_status()

            data = response.json()
            movie_ids = [movie["id"] for movie in data.get("results", [])]

            # 如果第一页不够，获取第二页
            if len(movie_ids) < limit and data.get("total_pages", 0) > 1:
                params["page"] = 2
                response = requests.get(
                    f"{cls.TMDB_BASE_URL}/discover/movie",
                    params=params,
                    headers=cls.TMDB_HEADERS,
                    timeout=10,
                )
                response.raise_for_status()
                data = response.json()
                movie_ids.extend([movie["id"] for movie in data.get("results", [])])

            return movie_ids[:limit]

        except Exception as e:
            print(f"Error fetching movies by genres: {e}")
            return cls._get_popular_movies(limit)

    @classmethod
    def _get_popular_movies(cls, limit=50):
        """
        获取热门电影作为后备方案
        """
        try:
            params = {"page": 1}

            response = requests.get(
                f"{cls.TMDB_BASE_URL}/movie/popular",
                params=params,
                headers=cls.TMDB_HEADERS,
                timeout=10,
            )
            response.raise_for_status()

            data = response.json()
            movie_ids = [movie["id"] for movie in data.get("results", [])]

            return movie_ids[:limit]

        except Exception as e:
            print(f"Error fetching popular movies: {e}")
            return []

    @classmethod
    def check_group_match(cls, group_session, tmdb_id):
        """
        检查是否所有活跃成员都喜欢这部电影

        Args:
            group_session: GroupSession 实例
            tmdb_id: 电影 ID

        Returns:
            bool: 是否匹配
        """
        # 获取活跃成员数量
        active_member_count = GroupMember.objects.filter(
            group_session=group_session, is_active=True
        ).count()

        # 获取喜欢这部电影的成员数量
        like_count = GroupSwipe.objects.filter(
            group_session=group_session, tmdb_id=tmdb_id, action=GroupSwipe.Action.LIKE
        ).count()

        print(
            f"[DEBUG check_group_match] active_members: {active_member_count}, likes: {like_count}, tmdb_id: {tmdb_id}"
        )

        # 检查是否所有人都喜欢
        is_match = like_count >= active_member_count and active_member_count > 0
        print(f"[DEBUG check_group_match] Result: {is_match}")
        return is_match

    @classmethod
    def get_movie_details(cls, tmdb_id):
        """
        从 TMDB 获取电影详情

        Args:
            tmdb_id: 电影 ID

        Returns:
            dict: 电影信息
        """
        cache_key = f"movie_details_{tmdb_id}"
        cached_data = cache.get(cache_key)

        if cached_data:
            return cached_data

        try:
            response = requests.get(
                f"{cls.TMDB_BASE_URL}/movie/{tmdb_id}",
                headers=cls.TMDB_HEADERS,
                timeout=10,
            )
            response.raise_for_status()

            data = response.json()

            # 格式化返回数据
            movie_info = {
                "tmdb_id": data["id"],
                "title": data.get("title", ""),
                "original_title": data.get("original_title", ""),
                "overview": data.get("overview", ""),
                "poster_path": data.get("poster_path", ""),
                "backdrop_path": data.get("backdrop_path", ""),
                "release_date": data.get("release_date", ""),
                "vote_average": data.get("vote_average", 0),
                "vote_count": data.get("vote_count", 0),
                "runtime": data.get("runtime", 0),
                "genres": [g["name"] for g in data.get("genres", [])],  # ← 类型名称列表
            }

            # 缓存 24 小时
            cache.set(cache_key, movie_info, 86400)

            return movie_info

        except Exception as e:
            print(f"Error fetching movie details: {e}")
            return None

    @classmethod
    def invalidate_deck_cache(cls, group_session):
        """
        清除群组推荐缓存（当有新的 swipe 或成员变化时调用）
        """
        cache_key = f"group_deck_{group_session.id}"
        cache.delete(cache_key)

    @classmethod
    def search_movies(cls, query, limit=10):
        """
        Search for movies by title using TMDb API

        Args:
            query: Movie title to search for
            limit: Maximum number of results to return

        Returns:
            list: List of movie dictionaries with id, title, year, poster_path
        """
        if not cls.TMDB_TOKEN:
            return []

        try:
            url = f"{cls.TMDB_BASE_URL}/search/movie"
            params = {
                "query": query,
                "language": "en-US",
                "page": 1,
                "include_adult": False,
            }

            response = requests.get(
                url, headers=cls.TMDB_HEADERS, params=params, timeout=10
            )
            response.raise_for_status()
            data = response.json()

            results = []
            for movie in data.get("results", [])[:limit]:
                results.append(
                    {
                        "tmdb_id": movie.get("id"),
                        "title": movie.get("title"),
                        "year": (
                            movie.get("release_date", "")[:4]
                            if movie.get("release_date")
                            else ""
                        ),
                        "poster_path": movie.get("poster_path"),
                        "overview": movie.get("overview", ""),
                        "vote_average": movie.get("vote_average", 0),
                    }
                )

            return results

        except Exception as e:
            print(f"Error searching movies: {e}")
            return []

    @classmethod
    def get_similar_movies(cls, tmdb_id, limit=20):
        """
        Get similar movies using TMDb's recommendations endpoint with filtering
        for more relevant and recent results. Only returns movies that share
        at least one genre with the original movie.

        Args:
            tmdb_id: TMDb movie ID
            limit: Maximum number of similar movies to return

        Returns:
            list: List of similar movie dictionaries
        """
        # Check cache first
        cache_key = f"similar_movies_{tmdb_id}"
        cached_similar = cache.get(cache_key)
        if cached_similar:
            return cached_similar[:limit]

        if not cls.TMDB_TOKEN:
            return []

        try:
            # First, get the original movie's genres
            movie_url = f"{cls.TMDB_BASE_URL}/movie/{tmdb_id}"
            movie_response = requests.get(
                movie_url, headers=cls.TMDB_HEADERS, timeout=10
            )
            movie_response.raise_for_status()
            original_movie = movie_response.json()
            original_genres = set(
                genre["id"] for genre in original_movie.get("genres", [])
            )

            # Use recommendations endpoint for better matches
            url = f"{cls.TMDB_BASE_URL}/movie/{tmdb_id}/recommendations"
            params = {"language": "en-US", "page": 1}

            response = requests.get(
                url, headers=cls.TMDB_HEADERS, params=params, timeout=10
            )
            response.raise_for_status()
            data = response.json()

            results = []
            for movie in data.get("results", []):
                # Get movie year
                release_date = movie.get("release_date", "")
                year = release_date[:4] if release_date else ""

                # Get movie genres
                movie_genre_ids = set(movie.get("genre_ids", []))

                # Filter criteria for more specific results:
                # 1. Must have a release year
                # 2. Movie must be from 2000 or newer (avoid very old films)
                # 3. Must have at least 100 votes (avoid obscure films)
                # 4. Must have rating of 5.0 or higher (avoid low-quality films)
                # 5. Must share at least one genre with the original movie
                if not year:
                    continue
                if int(year) < 2000:
                    continue
                if movie.get("vote_count", 0) < 100:
                    continue
                if movie.get("vote_average", 0) < 5.0:
                    continue
                # Check genre overlap - must share at least 2 genres for better relevance
                genre_overlap = original_genres.intersection(movie_genre_ids)
                if len(genre_overlap) < 2:
                    continue

                # Calculate genre match score (more shared genres = higher score)
                genre_match_score = len(genre_overlap)

                results.append(
                    {
                        "tmdb_id": movie.get("id"),
                        "title": movie.get("title"),
                        "year": year,
                        "poster_path": movie.get("poster_path"),
                        "overview": movie.get("overview", ""),
                        "vote_average": movie.get("vote_average", 0),
                        "backdrop_path": movie.get("backdrop_path"),
                        "genre_ids": movie.get("genre_ids", []),
                        "vote_count": movie.get("vote_count", 0),
                        "genre_match_score": genre_match_score,
                    }
                )

            # Sort by genre match score first, then by vote average
            results.sort(
                key=lambda x: (x["genre_match_score"], x["vote_average"]), reverse=True
            )

            # Cache for 1 hour
            cache.set(cache_key, results, cls.CACHE_TIMEOUT)

            return results[:limit]

        except Exception as e:
            print(f"Error fetching similar movies: {e}")
            return []

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
                "total_movies": 5,
            }

        # 固定每轮 20 部电影
        MOVIES_PER_ROUND = 5
        total_movies = MOVIES_PER_ROUND

        print(f"[DEBUG check_finished] Movies per round: {total_movies}")

        finished_members = 0

        # 检查每个成员
        for member in active_members:
            # 统计该成员的滑动次数
            swipe_count = GroupSwipe.objects.filter(
                group_session=group_session, user=member.user
            ).count()

            print(f"[DEBUG check_finished] User: {member.user.username}")
            print(f"[DEBUG check_finished]   - Total swipes: {swipe_count}")

            # 滑动次数 >= 20 = 完成
            if swipe_count >= MOVIES_PER_ROUND:
                print("[DEBUG check_finished]   - ✅ User FINISHED!")
                finished_members += 1
            else:
                print(
                    f"[DEBUG check_finished]   - ❌ NOT finished ({swipe_count}/{MOVIES_PER_ROUND})"
                )

        all_finished = (finished_members == total_members) and total_members > 0

        print(
            f"[DEBUG check_finished] Result: {finished_members}/{total_members} finished"
        )
        print(f"[DEBUG check_finished] All finished: {all_finished}")

        return {
            "all_finished": all_finished,
            "total_members": total_members,
            "finished_members": finished_members,
            "total_movies": total_movies,
        }

    @classmethod
    def get_all_common_matches(cls, group_session):
        """
        获取所有成员都喜欢的电影列表

        Args:
            group_session: GroupSession 实例

        Returns:
            list: 所有人都喜欢的电影列表
            [
                {
                    'tmdb_id': 550,
                    'movie_title': 'Fight Club',
                    'movie_info': {...},
                    'poster_url': '...',
                    'year': '1999',
                    'genres': ['Drama', 'Thriller'],
                    'overview': '...',
                    'vote_average': 8.4
                },
                ...
            ]
        """
        print(f"[DEBUG get_all_common_matches] Group: {group_session.group_code}")

        # 获取活跃成员数量
        active_members = GroupMember.objects.filter(
            group_session=group_session, is_active=True
        )
        total_members = active_members.count()

        print(f"[DEBUG get_all_common_matches] Total active members: {total_members}")

        if total_members == 0:
            return []

        # 查询所有 LIKE 的电影，按 tmdb_id 分组，统计每部电影的点赞数
        from django.db.models import Count

        common_movies = (
            GroupSwipe.objects.filter(
                group_session=group_session, action=GroupSwipe.Action.LIKE
            )
            .values("tmdb_id")
            .annotate(like_count=Count("id"))
            .filter(like_count=total_members)  # 所有人都喜欢
            .values_list("tmdb_id", flat=True)
        )

        common_movie_ids = list(common_movies)
        print(
            f"[DEBUG get_all_common_matches] Found {len(common_movie_ids)} common matches"
        )
        print(f"[DEBUG get_all_common_matches] Movie IDs: {common_movie_ids}")

        # 获取每部电影的详细信息
        result = []
        for tmdb_id in common_movie_ids:
            # 从缓存或 TMDB API 获取电影详情
            movie_info = cls.get_movie_details(tmdb_id)

            if movie_info:
                # 构建海报 URL
                poster_url = None
                if movie_info.get("poster_path"):
                    poster_url = (
                        f"https://image.tmdb.org/t/p/w500{movie_info['poster_path']}"
                    )

                # 处理类型
                genres_list = []
                if movie_info.get("genres"):
                    genres = movie_info["genres"]
                    if isinstance(genres, list) and len(genres) > 0:
                        if isinstance(genres[0], dict):
                            genres_list = [g.get("name", str(g)) for g in genres]
                        elif isinstance(genres[0], str):
                            genres_list = genres

                # 获取电影标题
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
        deleted_count = GroupSwipe.objects.filter(group_session=group_session).delete()[
            0
        ]

        print(
            f"[DEBUG clear_swipes] Cleared {deleted_count} swipe records for group {group_session.group_code}"
        )

        # 清除缓存
        cls.invalidate_deck_cache(group_session)

        return deleted_count


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
        from .models import UserPreference, Interaction
        from django.db import transaction
        from django.utils import timezone
        from datetime import timedelta

        # Get or create preference object
        preference, created = UserPreference.objects.get_or_create(user=user)

        # Skip if recently updated (unless force_recalculate)
        if not force_recalculate and not created:
            recent_threshold = timezone.now() - timedelta(minutes=5)
            if preference.last_updated > recent_threshold:
                return preference

        # Get all user interactions
        interactions = Interaction.objects.filter(user=user).select_related()

        # Calculate statistics
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

        # Calculate average rating
        ratings = interactions.exclude(rating__isnull=True).values_list(
            "rating", flat=True
        )
        average_rating = sum(ratings) / len(ratings) if ratings else None

        # Calculate genre preferences
        genre_weights = {}
        liked_movies = interactions.filter(
            status__in=[Interaction.Status.LIKE, Interaction.Status.WATCHED_LIKED]
        ).values_list("tmdb_id", flat=True)
        disliked_movies = interactions.filter(
            status__in=[Interaction.Status.DISLIKE, Interaction.Status.WATCHED_DISLIKED]
        ).values_list("tmdb_id", flat=True)

        # Process liked movies: +2 weight per genre
        for tmdb_id in liked_movies[:50]:  # Limit to avoid too many API calls
            movie_details = RecommendationService.get_movie_details(tmdb_id)
            if movie_details and movie_details.get("genres"):
                for genre_name in movie_details["genres"]:
                    genre_weights[genre_name] = genre_weights.get(genre_name, 0) + 2

        # Process disliked movies: -1 weight per genre
        for tmdb_id in disliked_movies[:20]:  # Limit to avoid too many API calls
            movie_details = RecommendationService.get_movie_details(tmdb_id)
            if movie_details and movie_details.get("genres"):
                for genre_name in movie_details["genres"]:
                    genre_weights[genre_name] = genre_weights.get(genre_name, 0) - 1

        # Normalize genre scores to 0.0-1.0 range
        genre_preferences = {}
        if genre_weights:
            min_weight = min(genre_weights.values())
            max_weight = max(genre_weights.values())
            weight_range = max_weight - min_weight if max_weight != min_weight else 1

            for genre, weight in genre_weights.items():
                # Normalize: (weight - min) / range
                normalized = (
                    (weight - min_weight) / weight_range if weight_range > 0 else 0.5
                )
                # Ensure it's between 0.0 and 1.0
                genre_preferences[genre] = max(0.0, min(1.0, normalized))

        # Extract preferred actors/directors from liked movies
        # Note: This requires additional API calls to get credits
        # For now, we'll skip this to avoid too many API calls
        # Can be enhanced later with a separate endpoint or background job
        preferred_actors = []
        preferred_directors = []

        # Optional: Fetch credits for top liked movies (can be expensive)
        # Uncomment if you want to track actors/directors
        # for tmdb_id in liked_movies[:10]:  # Limit to avoid API rate limits
        #     try:
        #         credits_url = f"{RecommendationService.TMDB_BASE_URL}/movie/{tmdb_id}/credits"
        #         response = requests.get(
        #             credits_url,
        #             headers=RecommendationService.TMDB_HEADERS,
        #             timeout=10,
        #         )
        #         if response.status_code == 200:
        #             credits = response.json()
        #             # Extract top 3 cast members
        #             cast = credits.get("cast", [])[:3]
        #             for actor in cast:
        #                 actor_id = actor.get("id")
        #                 if actor_id and actor_id not in preferred_actors:
        #                     preferred_actors.append(actor_id)
        #             # Extract directors
        #             crew = credits.get("crew", [])
        #             for person in crew:
        #                 if person.get("job") == "Director":
        #                     director_id = person.get("id")
        #                     if director_id and director_id not in preferred_directors:
        #                         preferred_directors.append(director_id)
        #     except Exception:
        #         pass  # Skip if API call fails

        # Update preference object
        with transaction.atomic():
            preference.genre_preferences = genre_preferences
            preference.preferred_actors = preferred_actors[:20]  # Limit to top 20
            preference.preferred_directors = preferred_directors[:10]  # Limit to top 10
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
        from .models import UserPreference

        try:
            preference = UserPreference.objects.get(user=user)
            return preference.genre_preferences or {}
        except UserPreference.DoesNotExist:
            return {}


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

    # Cache timeouts
    SIMILARITY_CACHE_TIMEOUT = 86400  # 24 hours (user similarities don't change often)
    RECOMMENDATION_CACHE_TIMEOUT = 3600  # 1 hour (recommendations per user)
    MIN_INTERACTIONS_FOR_CF = 3  # Minimum interactions needed for CF to work
    MIN_SIMILAR_USERS = 3  # Minimum similar users needed for recommendations
    SIMILARITY_THRESHOLD = 0.1  # Minimum similarity score (0.0-1.0)

    @classmethod
    def get_user_interaction_vector(cls, user):
        """
        Build interaction vector for a user.

        Returns a dict mapping tmdb_id -> interaction score:
        - LIKE: 2.0
        - WATCHED_LIKED: 2.0
        - DISLIKE: -1.0
        - WATCHED_DISLIKED: -1.0
        - WATCH_LATER: 0.5
        - WATCHED: 0.0 (neutral)

        Args:
            user: User instance

        Returns:
            dict: {tmdb_id: score, ...}
        """
        interactions = Interaction.objects.filter(user=user).select_related()

        vector = {}
        for interaction in interactions:
            tmdb_id = interaction.tmdb_id
            status = interaction.status

            # Map status to score
            if status in [Interaction.Status.LIKE, Interaction.Status.WATCHED_LIKED]:
                score = 2.0
            elif status in [
                Interaction.Status.DISLIKE,
                Interaction.Status.WATCHED_DISLIKED,
            ]:
                score = -1.0
            elif status == Interaction.Status.WATCH_LATER:
                score = 0.5
            else:  # WATCHED (neutral)
                score = 0.0

            # If user has a rating, incorporate it
            if interaction.rating:
                # Normalize rating (1-10) to (-1, 1) range
                normalized_rating = (interaction.rating - 5.5) / 4.5
                score = score + normalized_rating * 0.5

            # Accumulate scores if user has multiple interactions with same movie
            vector[tmdb_id] = vector.get(tmdb_id, 0.0) + score

        return vector

    @classmethod
    def cosine_similarity(cls, vector1, vector2):
        """
        Calculate cosine similarity between two interaction vectors.

        Args:
            vector1: dict of {tmdb_id: score, ...}
            vector2: dict of {tmdb_id: score, ...}

        Returns:
            float: Similarity score between 0.0 and 1.0
        """
        # Get intersection of movies both users interacted with
        common_movies = set(vector1.keys()) & set(vector2.keys())

        if not common_movies:
            return 0.0

        # Calculate dot product and magnitudes
        dot_product = sum(vector1[movie] * vector2[movie] for movie in common_movies)

        magnitude1 = math.sqrt(sum(score**2 for score in vector1.values()))
        magnitude2 = math.sqrt(sum(score**2 for score in vector2.values()))

        if magnitude1 == 0 or magnitude2 == 0:
            return 0.0

        # Cosine similarity: dot product / (magnitude1 * magnitude2)
        similarity = dot_product / (magnitude1 * magnitude2)

        # Normalize to 0.0-1.0 range (cosine similarity is -1 to 1, but with our scoring it's usually 0-1)
        return max(0.0, min(1.0, similarity))

    @classmethod
    def find_similar_users(cls, user, limit=20, min_similarity=None):
        """
        Find users similar to the given user based on interaction patterns.

        Args:
            user: User instance
            limit: Maximum number of similar users to return
            min_similarity: Minimum similarity threshold (default: SIMILARITY_THRESHOLD)

        Returns:
            list: List of tuples (similar_user, similarity_score) sorted by score descending
        """
        if min_similarity is None:
            min_similarity = cls.SIMILARITY_THRESHOLD

        # Check cache
        cache_key = f"similar_users_{user.id}"
        cached_similar = cache.get(cache_key)
        if cached_similar:
            return cached_similar[:limit]

        # Get user's interaction vector
        user_vector = cls.get_user_interaction_vector(user)

        if len(user_vector) < cls.MIN_INTERACTIONS_FOR_CF:
            # User doesn't have enough interactions for CF
            return []

        # Get all other users with interactions
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

        # Sort by similarity (descending)
        similar_users.sort(key=lambda x: x[1], reverse=True)

        # Cache results
        cache.set(cache_key, similar_users, cls.SIMILARITY_CACHE_TIMEOUT)

        return similar_users[:limit]

    @classmethod
    def get_collaborative_recommendations(cls, user, limit=50, min_similar_users=None):
        """
        Get movie recommendations using collaborative filtering.

        Strategy:
        1. Find similar users
        2. Get movies they liked (that current user hasn't seen)
        3. Score movies by weighted similarity (more similar users = higher score)
        4. Return top recommendations

        Args:
            user: User instance
            limit: Maximum number of recommendations
            min_similar_users: Minimum number of similar users needed (default: MIN_SIMILAR_USERS)

        Returns:
            list: List of tmdb_id recommendations sorted by score
        """
        if min_similar_users is None:
            min_similar_users = cls.MIN_SIMILAR_USERS

        # Check cache
        cache_key = f"cf_recommendations_{user.id}"
        cached_recs = cache.get(cache_key)
        if cached_recs:
            return cached_recs[:limit]

        # Find similar users
        similar_users = cls.find_similar_users(user, limit=50)

        if len(similar_users) < min_similar_users:
            # Not enough similar users for reliable recommendations
            return []

        # Get movies current user has already interacted with
        user_interactions = set(
            Interaction.objects.filter(user=user).values_list("tmdb_id", flat=True)
        )

        # Score movies based on similar users' preferences
        movie_scores = defaultdict(float)
        movie_counts = defaultdict(int)

        for similar_user, similarity_score in similar_users:
            # Get movies similar user liked
            liked_movies = Interaction.objects.filter(
                user=similar_user,
                status__in=[Interaction.Status.LIKE, Interaction.Status.WATCHED_LIKED],
            ).values_list("tmdb_id", flat=True)

            # Score each movie by similarity weight
            for tmdb_id in liked_movies:
                if tmdb_id not in user_interactions:
                    # Weight by similarity: more similar users = higher score
                    movie_scores[tmdb_id] += similarity_score
                    movie_counts[tmdb_id] += 1

        if not movie_scores:
            return []

        # Normalize scores by number of similar users who liked it
        # Movies liked by more similar users get higher scores
        for tmdb_id in movie_scores:
            # Average similarity score * log(count) to favor movies liked by multiple similar users
            count = movie_counts[tmdb_id]
            movie_scores[tmdb_id] = movie_scores[tmdb_id] * (1 + math.log(count + 1))

        # Sort by score and return top movies
        sorted_movies = sorted(movie_scores.items(), key=lambda x: x[1], reverse=True)
        recommendations = [tmdb_id for tmdb_id, _ in sorted_movies]

        # Cache results
        cache.set(cache_key, recommendations, cls.RECOMMENDATION_CACHE_TIMEOUT)

        return recommendations[:limit]

    @classmethod
    def get_hybrid_recommendations(
        cls, user, limit=50, cf_weight=0.4, preference_weight=0.4, popular_weight=0.2
    ):
        """
        Get hybrid recommendations combining collaborative filtering and preference-based approaches.

        Args:
            user: User instance
            limit: Maximum number of recommendations
            cf_weight: Weight for collaborative filtering (0.0-1.0)
            preference_weight: Weight for preference-based (0.0-1.0)
            popular_weight: Weight for popular movies fallback (0.0-1.0)

        Returns:
            list: List of tmdb_id recommendations
        """
        # Normalize weights
        total_weight = cf_weight + preference_weight + popular_weight
        if total_weight > 0:
            cf_weight /= total_weight
            preference_weight /= total_weight
            popular_weight /= total_weight

        # Check cache
        cache_key = f"hybrid_recommendations_{user.id}_{cf_weight}_{preference_weight}"
        cached_recs = cache.get(cache_key)
        if cached_recs:
            return cached_recs[:limit]

        all_movies = {}

        # 1. Get collaborative filtering recommendations
        cf_movies = cls.get_collaborative_recommendations(user, limit=limit * 2)
        for idx, tmdb_id in enumerate(cf_movies):
            score = (len(cf_movies) - idx) * cf_weight  # Higher rank = higher score
            all_movies[tmdb_id] = all_movies.get(tmdb_id, 0.0) + score

        # 2. Get preference-based recommendations
        try:
            from .models import UserPreference

            preference = UserPreference.objects.get(user=user)
            if preference.genre_preferences and preference.total_interactions > 0:
                pref_movies = RecommendationService._generate_solo_recommendations_from_preferences(
                    user, preference, limit * 2
                )
                for idx, tmdb_id in enumerate(pref_movies):
                    score = (len(pref_movies) - idx) * preference_weight
                    all_movies[tmdb_id] = all_movies.get(tmdb_id, 0.0) + score
        except Exception:
            pass  # Fallback if preferences don't exist

        # 3. Add popular movies as fallback (lower weight)
        if popular_weight > 0:
            popular_movies = RecommendationService._get_popular_movies(limit=limit)
            for idx, tmdb_id in enumerate(popular_movies):
                score = (
                    (len(popular_movies) - idx) * popular_weight * 0.5
                )  # Lower weight for popular
                all_movies[tmdb_id] = all_movies.get(tmdb_id, 0.0) + score

        # Sort by combined score
        sorted_movies = sorted(all_movies.items(), key=lambda x: x[1], reverse=True)
        recommendations = [tmdb_id for tmdb_id, _ in sorted_movies]

        # Cache results
        cache.set(cache_key, recommendations, cls.RECOMMENDATION_CACHE_TIMEOUT)

        return recommendations[:limit]

    @classmethod
    def invalidate_user_cache(cls, user):
        """
        Invalidate all cached data for a user (call when user interactions change).

        Args:
            user: User instance
        """
        cache_keys = [
            f"similar_users_{user.id}",
            f"cf_recommendations_{user.id}",
        ]

        # Also invalidate hybrid recommendations (pattern matching)
        # Note: Django cache doesn't support pattern deletion, so we'll clear common patterns
        for key in cache_keys:
            cache.delete(key)

        # Invalidate hybrid cache (approximate - clear all hybrid for this user)
        # In production, consider using cache versioning or Redis with pattern deletion
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

        Args:
            user_ids: List of user IDs to precompute (None = all active users)
            batch_size: Number of users to process at a time
        """
        if user_ids is None:
            # Get all users with at least MIN_INTERACTIONS_FOR_CF interactions
            user_ids = list(
                User.objects.annotate(interaction_count=Count("interactions"))
                .filter(interaction_count__gte=cls.MIN_INTERACTIONS_FOR_CF)
                .values_list("id", flat=True)
            )

        processed = 0
        for user_id in user_ids:
            try:
                user = User.objects.get(id=user_id)
                # This will compute and cache similarities
                cls.find_similar_users(user, limit=20)
                processed += 1

                if processed % batch_size == 0:
                    print(f"Processed {processed} users...")
            except User.DoesNotExist:
                continue

        return processed
