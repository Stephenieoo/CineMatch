# recom_sys_app/services/tmdb_client.py
"""
TMDB API client with two-level caching (L1 in-process + L2 Django cache).
All direct HTTP communication with The Movie Database API lives here.
"""
from django.core.cache import cache
import requests
import os
import time
import threading
import logging

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# L1 Cache: in-process dict with TTL (faster than Django cache for hot keys)
# L2 Cache: Django cache backend (configured in settings, persistent across
#           requests and supports Redis in production)
# Together these form the "multi-level cache pipeline" described in the design.
# ---------------------------------------------------------------------------
_L1: dict = {}
_L1_LOCK = threading.Lock()
_L1_DEFAULT_TTL = 30  # seconds — short so stale data expires quickly


def _l1_get(key: str):
    """Return cached value from L1 (in-process) cache, or None if missing/expired."""
    with _L1_LOCK:
        entry = _L1.get(key)
        if entry is not None:
            value, expire_at = entry
            if time.monotonic() < expire_at:
                return value
            del _L1[key]
    return None


def _l1_set(key: str, value, ttl: int = _L1_DEFAULT_TTL) -> None:
    """Store value in L1 cache with the given TTL (seconds)."""
    with _L1_LOCK:
        _L1[key] = (value, time.monotonic() + ttl)


class TmdbClient:
    """
    Thin client for The Movie Database (TMDB) API.
    Handles authentication, caching, and all HTTP calls to TMDB endpoints.
    """

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
    CACHE_TIMEOUT = 3600  # 1-hour default cache TTL

    # Niche genres that typically have fewer movies in TMDB
    _NICHE_GENRE_IDS = {10770, 10402, 37, 10752, 10751}  # TV Movie, Music, Western, War, Family

    # Static genre name → TMDB genre ID mapping
    _GENRE_MAP = {
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

    @classmethod
    def get_movie_details(cls, tmdb_id):
        """
        Fetch movie details from TMDB with two-level caching.

        Returns:
            dict with keys: tmdb_id, title, original_title, overview, poster_path,
                            backdrop_path, release_date, vote_average, vote_count,
                            runtime, genres (list of name strings)
            None on error.
        """
        cache_key = f"movie_details_{tmdb_id}"
        t0 = time.monotonic()

        cached_data = _l1_get(cache_key)
        if cached_data is not None:
            _logger.debug(
                "[Cache L1 HIT] %s in %.2fms", cache_key, (time.monotonic() - t0) * 1000
            )
            return cached_data

        cached_data = cache.get(cache_key)
        if cached_data is not None:
            _l1_set(cache_key, cached_data)
            _logger.debug(
                "[Cache L2 HIT] %s in %.2fms", cache_key, (time.monotonic() - t0) * 1000
            )
            return cached_data

        try:
            t_api = time.monotonic()
            response = requests.get(
                f"{cls.TMDB_BASE_URL}/movie/{tmdb_id}",
                headers=cls.TMDB_HEADERS,
                timeout=10,
            )
            response.raise_for_status()
            api_ms = (time.monotonic() - t_api) * 1000

            data = response.json()
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
                "genres": [g["name"] for g in data.get("genres", [])],
            }

            cache.set(cache_key, movie_info, 86400)  # L2: 24-hour TTL
            _l1_set(cache_key, movie_info)  # L1: 30-second TTL
            _logger.info(
                "[Cache MISS] %s — TMDB API %.2fms (total %.2fms)",
                cache_key,
                api_ms,
                (time.monotonic() - t0) * 1000,
            )
            return movie_info

        except Exception as e:
            print(f"Error fetching movie details: {e}")
            return None

    @classmethod
    def search_movies(cls, query, limit=10):
        """
        Search for movies by title using the TMDB search endpoint.

        Returns:
            list of dicts: tmdb_id, title, year, poster_path, overview, vote_average
        """
        if not cls.TMDB_TOKEN:
            return []

        try:
            response = requests.get(
                f"{cls.TMDB_BASE_URL}/search/movie",
                headers=cls.TMDB_HEADERS,
                params={
                    "query": query,
                    "language": "en-US",
                    "page": 1,
                    "include_adult": False,
                },
                timeout=10,
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
        Get movies similar to the given movie using TMDB recommendations + similar endpoints.
        Results are filtered by genre overlap and quality thresholds, then cached.

        Returns:
            list of dicts: tmdb_id, title, year, poster_path, overview, vote_average,
                           backdrop_path, genre_ids, vote_count, similarity_score
        """
        cache_key = f"similar_movies_{tmdb_id}"
        t0 = time.monotonic()

        cached_similar = _l1_get(cache_key)
        if cached_similar is not None:
            _logger.debug(
                "[Cache L1 HIT] %s in %.2fms", cache_key, (time.monotonic() - t0) * 1000
            )
            return cached_similar[:limit]

        cached_similar = cache.get(cache_key)
        if cached_similar is not None:
            _l1_set(cache_key, cached_similar)
            _logger.debug(
                "[Cache L2 HIT] %s in %.2fms", cache_key, (time.monotonic() - t0) * 1000
            )
            return cached_similar[:limit]

        t_api = time.monotonic()

        if not cls.TMDB_TOKEN:
            return []

        try:
            # Fetch original movie genres for overlap filtering
            movie_response = requests.get(
                f"{cls.TMDB_BASE_URL}/movie/{tmdb_id}",
                headers=cls.TMDB_HEADERS,
                timeout=10,
            )
            movie_response.raise_for_status()
            original_genres = set(
                genre["id"] for genre in movie_response.json().get("genres", [])
            )

            all_results = []
            target_results = max(limit * 2, 50)

            # Primary: recommendations endpoint (up to 5 pages)
            for page in range(1, 6):
                response = requests.get(
                    f"{cls.TMDB_BASE_URL}/movie/{tmdb_id}/recommendations",
                    headers=cls.TMDB_HEADERS,
                    params={"language": "en-US", "page": page},
                    timeout=10,
                )
                response.raise_for_status()
                page_results = response.json().get("results", [])
                if not page_results:
                    break

                for movie in page_results:
                    entry = cls._score_similar_movie(movie, original_genres)
                    if entry is None:
                        continue
                    if any(r["tmdb_id"] == entry["tmdb_id"] for r in all_results):
                        continue
                    all_results.append(entry)
                    if len(all_results) >= target_results:
                        break

                if len(all_results) >= target_results:
                    break

            # Fallback: similar endpoint if not enough results
            if len(all_results) < limit:
                print(
                    f"[DEBUG] Only found {len(all_results)} from recommendations, trying similar endpoint..."
                )
                for page in range(1, 3):
                    similar_response = requests.get(
                        f"{cls.TMDB_BASE_URL}/movie/{tmdb_id}/similar",
                        headers=cls.TMDB_HEADERS,
                        params={"language": "en-US", "page": page},
                        timeout=10,
                    )
                    similar_response.raise_for_status()
                    similar_results = similar_response.json().get("results", [])
                    if not similar_results:
                        break

                    for movie in similar_results:
                        entry = cls._score_similar_movie(movie, original_genres)
                        if entry is None:
                            continue
                        if any(r["tmdb_id"] == entry["tmdb_id"] for r in all_results):
                            continue
                        all_results.append(entry)
                        if len(all_results) >= target_results:
                            break

                    if len(all_results) >= target_results:
                        break

            all_results.sort(key=lambda x: x["similarity_score"], reverse=True)

            cache.set(cache_key, all_results, cls.CACHE_TIMEOUT)  # L2: 1-hour TTL
            _l1_set(cache_key, all_results)  # L1: 30-second TTL
            _logger.info(
                "[Cache MISS] %s — TMDB API %.2fms (total %.2fms)",
                cache_key,
                (time.monotonic() - t_api) * 1000,
                (time.monotonic() - t0) * 1000,
            )
            return all_results[:limit]

        except Exception as e:
            print(f"Error fetching similar movies: {e}")
            import traceback
            traceback.print_exc()
            return []

    @classmethod
    def _score_similar_movie(cls, movie, original_genres):
        """
        Apply quality filters and compute a similarity score for a candidate movie.

        Returns a result dict or None if the movie should be excluded.
        """
        release_date = movie.get("release_date", "")
        year = release_date[:4] if release_date else ""
        if not year:
            return None
        try:
            if int(year) < 1990:
                return None
        except ValueError:
            return None
        if movie.get("vote_count", 0) < 20:
            return None
        if movie.get("vote_average", 0) < 4.0:
            return None

        movie_genre_ids = set(movie.get("genre_ids", []))
        genre_overlap = original_genres.intersection(movie_genre_ids)
        if len(genre_overlap) < 1:
            return None

        genre_match_score = len(genre_overlap)
        rating_score = movie.get("vote_average", 0) / 10.0
        vote_score = min(movie.get("vote_count", 0) / 1000.0, 1.0)
        similarity_score = genre_match_score * 0.5 + rating_score * 0.3 + vote_score * 0.2

        return {
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
            "similarity_score": similarity_score,
        }

    @classmethod
    def _get_movies_by_genres(cls, genre_ids, limit=100, randomize=True):
        """
        Fetch movies from TMDB matching any of the given genre IDs.
        Fetches from multiple pages and sort orders for variety.
        Uses OR logic (movies matching ANY selected genre).
        Handles niche genres with lower vote_count thresholds.
        """
        import random

        try:
            is_niche_genre = any(gid in cls._NICHE_GENRE_IDS for gid in genre_ids)
            genre_str = "|".join(map(str, genre_ids))  # OR logic

            all_movie_ids = []
            pages_per_sort = max(15, (limit // 20) + 10)

            sort_options = [
                "popularity.desc",
                "vote_average.desc",
                "release_date.desc",
                "vote_count.desc",
            ]

            for sort_by in sort_options:
                if len(all_movie_ids) >= limit * 3:
                    break

                start_page = random.randint(1, 5)
                for page in range(start_page, start_page + 10):
                    if len(all_movie_ids) >= limit * 3:
                        break

                    if is_niche_genre:
                        if page <= 3:
                            vote_threshold = 10
                        elif page <= 8:
                            vote_threshold = 5
                        else:
                            vote_threshold = None
                    else:
                        vote_threshold = 100 if page <= 5 else 50 if page <= 10 else 20

                    params = {
                        "with_genres": genre_str,
                        "sort_by": sort_by,
                        "page": page,
                        "include_adult": "false",
                        "language": "en-US",
                    }
                    if vote_threshold is not None:
                        params["vote_count.gte"] = vote_threshold

                    try:
                        response = requests.get(
                            f"{cls.TMDB_BASE_URL}/discover/movie",
                            params=params,
                            headers=cls.TMDB_HEADERS,
                            timeout=10,
                        )
                        response.raise_for_status()
                        data = response.json()
                        page_movies = [movie["id"] for movie in data.get("results", [])]

                        if not page_movies:
                            break

                        all_movie_ids.extend(page_movies)

                        total_pages = data.get("total_pages", 1)
                        if len(all_movie_ids) >= limit * 3 or page >= total_pages:
                            break
                    except Exception:
                        continue

            # Deduplicate while preserving order
            seen = set()
            unique_movies = []
            for movie_id in all_movie_ids:
                if movie_id not in seen:
                    seen.add(movie_id)
                    unique_movies.append(movie_id)

            # Fetch from each genre individually if still not enough
            if len(unique_movies) < limit:
                for genre_id in genre_ids:
                    if len(unique_movies) >= limit * 3:
                        break
                    try:
                        is_single_niche = genre_id in cls._NICHE_GENRE_IDS
                        single_genre_params = {
                            "with_genres": str(genre_id),
                            "sort_by": "popularity.desc",
                            "include_adult": "false",
                            "language": "en-US",
                        }
                        if not is_single_niche:
                            single_genre_params["vote_count.gte"] = 20

                        max_pages = 20 if is_single_niche else 15
                        for page in range(1, max_pages + 1):
                            if len(unique_movies) >= limit * 3:
                                break
                            if is_single_niche and page > 5:
                                single_genre_params.pop("vote_count.gte", None)
                            single_genre_params["page"] = page

                            response = requests.get(
                                f"{cls.TMDB_BASE_URL}/discover/movie",
                                params=single_genre_params,
                                headers=cls.TMDB_HEADERS,
                                timeout=10,
                            )
                            response.raise_for_status()
                            data = response.json()
                            page_movies = [movie["id"] for movie in data.get("results", [])]

                            if not page_movies:
                                break

                            for movie_id in page_movies:
                                if movie_id not in seen:
                                    seen.add(movie_id)
                                    unique_movies.append(movie_id)
                                    if len(unique_movies) >= limit * 3:
                                        break

                            if page >= data.get("total_pages", 1):
                                break
                    except Exception:
                        continue

            if randomize and len(unique_movies) > limit:
                random.shuffle(unique_movies)

            return unique_movies[:limit]

        except Exception as e:
            print(f"Error fetching movies by genres: {e}")
            return cls._get_popular_movies(limit, randomize=randomize)

    @classmethod
    def _get_popular_movies(cls, limit=50, randomize=True):
        """
        Fetch trending/popular movies from TMDB as a fallback.
        Uses random start pages for variety across calls.
        """
        import random

        try:
            all_movie_ids = []
            pages_to_fetch = min(5, (limit // 20) + 2)
            start_page = random.randint(1, 5)

            for page in range(start_page, start_page + pages_to_fetch):
                try:
                    response = requests.get(
                        f"{cls.TMDB_BASE_URL}/movie/popular",
                        params={"page": page},
                        headers=cls.TMDB_HEADERS,
                        timeout=10,
                    )
                    response.raise_for_status()
                    data = response.json()
                    page_movies = [movie["id"] for movie in data.get("results", [])]
                    all_movie_ids.extend(page_movies)

                    if len(all_movie_ids) >= limit * 2 or page >= data.get("total_pages", 1):
                        break
                except Exception:
                    continue

            seen = set()
            unique_movies = []
            for movie_id in all_movie_ids:
                if movie_id not in seen:
                    seen.add(movie_id)
                    unique_movies.append(movie_id)

            if randomize and len(unique_movies) > limit:
                random.shuffle(unique_movies)

            return unique_movies[:limit]

        except Exception as e:
            print(f"Error fetching popular movies: {e}")
            return []

    @classmethod
    def _get_genre_ids_by_names(cls, genre_names):
        """Convert a list of genre name strings to TMDB genre IDs."""
        return [cls._GENRE_MAP[name] for name in genre_names if name in cls._GENRE_MAP]
