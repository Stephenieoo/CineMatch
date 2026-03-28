# recom_sys_app/views_movie.py
"""
TMDB helper functions and movie-related views.

Contains:
  - TMDB configuration constants
  - Low-level TMDB helper functions (_tmdb_search, _tmdb_details, etc.)
  - Movie views: movie_details_view, search_movies_view
  - Movie search views: movie_search_view, search_movies_api, autocomplete_movies_api, get_similar_movies_api
  - Region API views: get_user_region_api, set_user_region_api
"""
import os
import re
import json
import requests

from django.http import JsonResponse
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods
from dotenv import load_dotenv
from django.conf import settings

from .models import Interaction
from .services import RecommendationService
from .geolocation import (
    get_user_region,
    set_user_region,
    get_all_regions,
    SUPPORTED_REGIONS,
)

load_dotenv(settings.BASE_DIR / ".env")

# ============================================
# TMDB Configuration
# ============================================

TMDB_TOKEN = (os.getenv("TMDB_TOKEN") or os.getenv("TMDB_API_KEY") or "").strip()
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_HEADERS = {
    "Authorization": f"Bearer {TMDB_TOKEN}",
    "Accept": "application/json",
}
IMG_BASE = "https://image.tmdb.org/t/p/w500"


# ============================================
# TMDB Helper Functions
# ============================================


def _normalize_title(s: str) -> str:
    """Normalize movie title for comparison"""
    s = s.lower().strip()
    s = re.sub(r"[\W_]+", "", s)
    return s


def _pick_best_hit(results, query_title: str):
    """Pick the best matching movie from TMDB search results"""
    qn = _normalize_title(query_title)
    best, best_score = None, -1

    for r in results:
        title = r.get("title") or r.get("original_title") or ""
        tn = _normalize_title(title)
        rd = r.get("release_date") or ""
        year = int(rd[:4]) if rd[:4].isdigit() else 0
        pop = float(r.get("popularity") or 0)

        # Scoring algorithm: exact match + recent + popularity
        score = (
            (100.0 if tn == qn else 0.0)
            + (20.0 if year >= 2020 else 0.0)
            + (pop / 50.0)
        )

        if score > best_score:
            best_score, best = score, r

    return best


def _tmdb_search(title: str):
    """Search for a movie on TMDB"""
    if not TMDB_TOKEN:
        raise RuntimeError("TMDB_TOKEN missing in .env")

    r = requests.get(
        f"{TMDB_BASE}/search/movie",
        params={"query": title, "include_adult": "True", "language": "en-US"},
        headers=TMDB_HEADERS,
        timeout=10,
    )
    r.raise_for_status()

    results = r.json().get("results") or []
    if not results:
        return None

    return _pick_best_hit(results, title)


def _tmdb_details(movie_id: int, append: str = "videos,credits"):
    """Get detailed information about a movie from TMDB"""
    r = requests.get(
        f"{TMDB_BASE}/movie/{movie_id}",
        params={"append_to_response": append} if append else {},
        headers=TMDB_HEADERS,
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def _tmdb_watch_providers(movie_id: int, region: str = "US"):
    """
    Get streaming platform availability for a movie from TMDB.
    Returns watch provider data (Netflix, Hulu, etc.) for the specified region.

    Args:
        movie_id: TMDB movie ID
        region: ISO 3166-1 alpha-2 country code (e.g., "US", "GB", "IN")

    Returns:
        dict with flatrate, rent, buy providers and JustWatch link
    """
    try:
        r = requests.get(
            f"{TMDB_BASE}/movie/{movie_id}/watch/providers",
            headers=TMDB_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()

        # Extract providers for the specified region
        results = data.get("results", {}).get(region, {})

        # If no providers found for the user's region, return empty but note the region
        return {
            "region": region,
            "flatrate": results.get(
                "flatrate", []
            ),  # Subscription services (Netflix, Disney+, etc.)
            "rent": results.get(
                "rent", []
            ),  # Rental options (iTunes, Google Play, etc.)
            "buy": results.get("buy", []),  # Purchase options
            "link": results.get("link", ""),  # JustWatch link
            "available": bool(results),  # Whether movie is available in this region
        }
    except Exception:
        # Return empty dict if API call fails
        return {
            "region": region,
            "flatrate": [],
            "rent": [],
            "buy": [],
            "link": "",
            "available": False,
        }


def _tmdb_fetch_all(titles: list[str]) -> list[dict]:
    """
    Fetch TMDB details for multiple movie titles.
    Returns a list of movie dictionaries with metadata.
    """
    out = []

    for q in titles:
        if not q:
            continue

        try:
            hit = _tmdb_search(q)
            if not hit:
                out.append({"query": q, "found": False, "reason": "No TMDB results"})
                continue

            det = _tmdb_details(hit["id"])
            out.append(
                {
                    "query": q,
                    "found": True,
                    "title": det.get("title") or hit.get("title") or q,
                    "tmdb_id": det.get("id"),
                    "year": (det.get("release_date") or "")[:4],
                    "overview": det.get("overview"),
                    "vote_average": det.get("vote_average"),
                    "vote_count": det.get("vote_count"),
                    "poster_url": (
                        (IMG_BASE + det["poster_path"])
                        if det.get("poster_path")
                        else None
                    ),
                    "backdrop_url": (
                        (IMG_BASE + det["backdrop_path"])
                        if det.get("backdrop_path")
                        else None
                    ),
                    "genres": [g.get("name") for g in det.get("genres", [])],
                    "runtime": det.get("runtime"),
                }
            )
        except Exception as e:
            out.append({"query": q, "found": False, "reason": f"Error: {str(e)}"})

    return out


def _tmdb_fetch_by_ids(movie_ids: list[int]) -> list[dict]:
    """
    Fetch TMDB details for multiple movie IDs.
    Returns a list of movie dictionaries with metadata.
    """
    out = []

    for tmdb_id in movie_ids:
        if not tmdb_id:
            continue

        try:
            det = _tmdb_details(tmdb_id)
            out.append(
                {
                    "found": True,
                    "title": det.get("title", ""),
                    "tmdb_id": det.get("id"),
                    "year": (det.get("release_date") or "")[:4],
                    "overview": det.get("overview"),
                    "vote_average": det.get("vote_average"),
                    "vote_count": det.get("vote_count"),
                    "poster_url": (
                        (IMG_BASE + det["poster_path"])
                        if det.get("poster_path")
                        else None
                    ),
                    "backdrop_url": (
                        (IMG_BASE + det["backdrop_path"])
                        if det.get("backdrop_path")
                        else None
                    ),
                    "genres": [g.get("name") for g in det.get("genres", [])],
                    "runtime": det.get("runtime"),
                }
            )
        except Exception as e:
            print(f"Error fetching movie {tmdb_id}: {e}")
            continue

    return out


# ============================================
# Movie Views
# ============================================


@login_required
def movie_details_view(request, tmdb_id: int):
    """
    Get detailed information about a specific movie.
    Includes TMDB data and user's interaction status.
    Uses user's detected/preferred region for watch providers.
    """
    try:
        movie_data = _tmdb_details(tmdb_id, append="videos,credits,recommendations")

        # Get user's region for watch providers
        user_region = get_user_region(request)

        # Fetch watch providers (where to watch) for user's region
        watch_providers = _tmdb_watch_providers(tmdb_id, region=user_region)

        # Check if user has interacted with this movie
        interaction = None
        try:
            interaction = Interaction.objects.get(user=request.user, tmdb_id=tmdb_id)
        except Interaction.DoesNotExist:
            pass
        # this is the code for the movie details view
        # Extract cast and crew information
        cast = movie_data.get("credits", {}).get("cast", [])
        crew = movie_data.get("credits", {}).get("crew", [])

        # Get main actors (top 5)
        main_actors = [
            {
                "name": actor.get("name"),
                "character": actor.get("character"),
                "profile_path": (
                    (IMG_BASE + actor["profile_path"])
                    if actor.get("profile_path")
                    else None
                ),
            }
            for actor in cast[:5]
        ]

        # Get director
        director = None
        for person in crew:
            if person.get("job") == "Director":
                director = {
                    "name": person.get("name"),
                    "profile_path": (
                        (IMG_BASE + person["profile_path"])
                        if person.get("profile_path")
                        else None
                    ),
                }
                break

        # Format response
        response_data = {
            "success": True,
            "movie": {
                "tmdb_id": movie_data.get("id"),
                "title": movie_data.get("title"),
                "original_title": movie_data.get("original_title"),
                "overview": movie_data.get("overview"),
                "release_date": movie_data.get("release_date"),
                "runtime": movie_data.get("runtime"),
                "vote_average": movie_data.get("vote_average"),
                "vote_count": movie_data.get("vote_count"),
                "popularity": movie_data.get("popularity"),
                "poster_url": (
                    (IMG_BASE + movie_data["poster_path"])
                    if movie_data.get("poster_path")
                    else None
                ),
                "backdrop_url": (
                    (IMG_BASE + movie_data["backdrop_path"])
                    if movie_data.get("backdrop_path")
                    else None
                ),
                "genres": [g.get("name") for g in movie_data.get("genres", [])],
                "tagline": movie_data.get("tagline"),
                "cast": main_actors,  # this is the main actors for the movie details view
                "director": director,  # this is the director for the movie details view
                "watch_providers": watch_providers,  # WHERE TO WATCH INTEGRATION
            },
            "user_interaction": (
                {
                    "status": interaction.status if interaction else None,
                    "rating": interaction.rating if interaction else None,
                }
                if interaction
                else None
            ),
        }

        return JsonResponse(response_data)

    except requests.HTTPError as e:
        return JsonResponse(
            {"success": False, "error": f"TMDB API error: {e.response.status_code}"},
            status=502,
        )
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
def search_movies_view(request):
    """
    Search for movies using TMDB API.
    Query parameter: ?q=movie+title
    """
    query = request.GET.get("q", "").strip()

    if not query:
        return JsonResponse(
            {"success": False, "error": "Query parameter 'q' is required"}, status=400
        )

    try:
        if not TMDB_TOKEN:
            raise RuntimeError("TMDB_TOKEN missing in .env")

        r = requests.get(
            f"{TMDB_BASE}/search/movie",
            params={
                "query": query,
                "include_adult": "False",
                "language": "en-US",
                "page": 1,
            },
            headers=TMDB_HEADERS,
            timeout=10,
        )
        r.raise_for_status()

        results = r.json().get("results", [])

        # Format results
        formatted_results = [
            {
                "tmdb_id": movie.get("id"),
                "title": movie.get("title"),
                "release_date": movie.get("release_date"),
                "year": movie.get("release_date", "")[:4],
                "overview": movie.get("overview"),
                "vote_average": movie.get("vote_average"),
                "poster_url": (
                    (IMG_BASE + movie["poster_path"])
                    if movie.get("poster_path")
                    else None
                ),
            }
            for movie in results[:10]
        ]  # Limit to top 10 results

        return JsonResponse(
            {
                "success": True,
                "query": query,
                "count": len(formatted_results),
                "results": formatted_results,
            }
        )

    except requests.HTTPError as e:
        return JsonResponse(
            {"success": False, "error": f"TMDB API error: {e.response.status_code}"},
            status=502,
        )
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


# ============================================
# Movie Search - Find Similar Movies
# ============================================


@login_required
def movie_search_view(request):
    """
    Renders the movie search page where users can search for a movie
    and find similar recommendations.
    """
    return render(request, "recom_sys_app/movie_search.html")


@login_required
@require_http_methods(["POST"])
def search_movies_api(request):
    """
    API endpoint to search for movies by title
    """
    try:
        data = json.loads(request.body)
        query = data.get("query", "").strip()

        if not query:
            return JsonResponse(
                {"success": False, "message": "Query is required"}, status=400
            )

        # Use RecommendationService to search
        results = RecommendationService.search_movies(query, limit=10)

        return JsonResponse(
            {"success": True, "results": results, "count": len(results)}
        )

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"success": False, "message": str(e)}, status=500)


@login_required
@require_http_methods(["GET"])
def autocomplete_movies_api(request):
    """
    Lightweight API endpoint for Algolia-style movie autocomplete.
    Returns multiple relevant movie suggestions as user types.

    Query params:
        q: Search query string (required)
        limit: Max results to return (default: 8)
    """
    query = request.GET.get("q", "").strip()

    if not query or len(query) < 2:
        return JsonResponse({"success": True, "results": [], "count": 0})

    try:
        limit = min(int(request.GET.get("limit", 8)), 15)  # Cap at 15
    except (ValueError, TypeError):
        limit = 8

    try:
        if not TMDB_TOKEN:
            return JsonResponse(
                {"success": False, "message": "TMDB API not configured"}, status=500
            )

        # Use TMDB search API for fast autocomplete
        r = requests.get(
            f"{TMDB_BASE}/search/movie",
            params={
                "query": query,
                "include_adult": "False",
                "language": "en-US",
                "page": 1,
            },
            headers=TMDB_HEADERS,
            timeout=5,  # Short timeout for autocomplete
        )
        r.raise_for_status()

        results = r.json().get("results", [])

        # Format results for autocomplete dropdown
        formatted_results = []
        for movie in results[:limit]:
            release_date = movie.get("release_date", "")
            year = release_date[:4] if release_date else ""

            formatted_results.append(
                {
                    "tmdb_id": movie.get("id"),
                    "title": movie.get("title"),
                    "year": year,
                    "vote_average": round(movie.get("vote_average", 0), 1),
                    "poster_path": movie.get("poster_path"),
                    "poster_url": (
                        f"{IMG_BASE}{movie['poster_path']}"
                        if movie.get("poster_path")
                        else None
                    ),
                }
            )

        return JsonResponse(
            {
                "success": True,
                "query": query,
                "count": len(formatted_results),
                "results": formatted_results,
            }
        )

    except requests.Timeout:
        return JsonResponse({"success": False, "message": "Search timeout"}, status=504)
    except requests.HTTPError as e:
        return JsonResponse(
            {"success": False, "message": f"TMDB API error: {e.response.status_code}"},
            status=502,
        )
    except Exception as e:
        return JsonResponse({"success": False, "message": str(e)}, status=500)


@login_required
@require_http_methods(["GET"])
def get_similar_movies_api(request, tmdb_id):
    """
    API endpoint to get similar movies for a given movie ID
    """
    try:
        # Increase default limit to return more movies
        limit = int(request.GET.get("limit", 30))

        # Use RecommendationService to get similar movies
        results = RecommendationService.get_similar_movies(tmdb_id, limit=limit)

        return JsonResponse(
            {
                "success": True,
                "results": results,
                "count": len(results),
                "tmdb_id": tmdb_id,
            }
        )

    except Exception as e:
        return JsonResponse({"success": False, "message": str(e)}, status=500)


# ============================================
# Region / Geolocation API Endpoints
# ============================================


@login_required
@require_http_methods(["GET"])
def get_user_region_api(request):
    """
    Get the user's detected or preferred region.
    Used for filtering movies by regional availability.

    Returns:
        JSON with region code, name, and available regions list
    """
    from .geolocation import REGION_NAMES

    region = get_user_region(request)

    return JsonResponse(
        {
            "success": True,
            "region": {
                "code": region,
                "name": REGION_NAMES.get(region, region),
            },
            "available_regions": get_all_regions(),
        }
    )


@login_required
@require_http_methods(["POST"])
def set_user_region_api(request):
    """
    Manually set the user's preferred region.
    Allows users to override the auto-detected region.

    Body: {"region": "US"}
    """
    try:
        data = json.loads(request.body)
        region_code = data.get("region", "").strip().upper()

        if not region_code:
            return JsonResponse(
                {"success": False, "message": "Region code is required"}, status=400
            )

        if region_code not in SUPPORTED_REGIONS:
            return JsonResponse(
                {"success": False, "message": f"Invalid region code: {region_code}"},
                status=400,
            )

        set_user_region(request, region_code)

        from .geolocation import REGION_NAMES

        return JsonResponse(
            {
                "success": True,
                "message": f"Region set to {REGION_NAMES.get(region_code, region_code)}",
                "region": {
                    "code": region_code,
                    "name": REGION_NAMES.get(region_code, region_code),
                },
            }
        )

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"success": False, "message": str(e)}, status=500)
