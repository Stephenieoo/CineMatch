# recom_sys_app/views.py
"""
Core template-based views: profile, communities, edit profile,
interactions, recommendations, home, health check, user stats.

TMDB helpers, movie views, and search views → views_movie.py
AI agent and user affinity helpers          → ai_agent.py
Group CRUD and management                   → views_group.py
Community management                        → views_community.py
"""
import json
import requests

from django.http import JsonResponse, HttpResponse
from django.conf import settings
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from django.middleware.csrf import get_token
from django.contrib.auth.decorators import login_required

from .forms import UserProfileForm
from .models import UserProfile, Interaction, GroupSession, GroupMember
from .services import RecommendationService
from .ai_agent import _get_signup_movies, _get_signup_genre
from .views_movie import _tmdb_fetch_by_ids


# ============================================
# Profile & Communities
# ============================================


@login_required
def profile_view(request):
    """
    User profile dashboard view.
    Display user info and group options (no edit form).
    """
    profile, _ = UserProfile.objects.get_or_create(
        user=request.user, defaults={"name": request.user.username}
    )

    # Get user's PRIVATE group memberships only (exclude communities)
    user_groups = (
        GroupMember.objects.filter(
            user=request.user,
            is_active=True,
            group_session__kind=GroupSession.Kind.PRIVATE,
        )
        .select_related("group_session")
        .order_by("-joined_at")
    )

    # Get created PRIVATE groups only (exclude communities)
    created_groups = GroupSession.objects.filter(
        creator=request.user, is_active=True, kind=GroupSession.Kind.PRIVATE
    ).order_by("-created_at")

    # Fetch personalized movie recommendations for preview sections
    try:
        # Solo Mode - Get personalized recommendations based on user preferences
        # Request more than needed to ensure we have 14 after filtering
        solo_movie_ids = RecommendationService.get_solo_deck(request.user, limit=20)
        print(f"[DEBUG profile_view] Solo movie IDs: {solo_movie_ids}")

        if solo_movie_ids:
            # Fetch up to 20 and take first 14 that load successfully
            solo_movies = _tmdb_fetch_by_ids(solo_movie_ids[:20])
            solo_movies = solo_movies[:14]  # Limit to 14
            print(f"[DEBUG profile_view] Solo movies fetched: {len(solo_movies)}")
        else:
            # Fallback to popular movies if no recommendations
            print("[DEBUG profile_view] No solo recommendations, using popular movies")
            fallback_ids = RecommendationService._get_popular_movies(limit=20)
            solo_movies = _tmdb_fetch_by_ids(fallback_ids[:20]) if fallback_ids else []
            solo_movies = solo_movies[:14]  # Limit to 14

        # If we still don't have enough movies, try to get more popular ones
        if len(solo_movies) < 14:
            print(
                f"[DEBUG profile_view] Only got {len(solo_movies)} movies, fetching more popular movies"
            )
            additional_ids = RecommendationService._get_popular_movies(limit=20)
            existing_ids = {m.get("tmdb_id") for m in solo_movies}
            additional_ids = [mid for mid in additional_ids if mid not in existing_ids]
            additional_movies = _tmdb_fetch_by_ids(
                additional_ids[: 14 - len(solo_movies)]
            )
            solo_movies.extend(additional_movies)
            solo_movies = solo_movies[:14]

        # Private Groups - No placeholder movies, will load when user creates/joins a group
        group_movies = []

        # Communities - Get one movie per genre for preview (7-8 genres)
        community_genres = [
            {"name": "Action", "id": 28},
            {"name": "Horror", "id": 27},
            {"name": "Comedy", "id": 35},
            {"name": "Romance", "id": 10749},
            {"name": "Science Fiction", "id": 878},
            {"name": "Thriller", "id": 53},
            {"name": "Drama", "id": 18},
            {"name": "Animation", "id": 16},
        ]

        community_previews = []
        for genre in community_genres:
            try:
                genre_movie_ids = RecommendationService._get_movies_by_genres(
                    [genre["id"]], limit=1
                )
                if genre_movie_ids:
                    movies = _tmdb_fetch_by_ids([genre_movie_ids[0]])
                    if movies:
                        community_previews.append(
                            {
                                "genre": genre["name"],
                                "genre_id": genre["id"],
                                "movie": movies[0],
                            }
                        )
            except Exception as e:
                print(f"Error fetching preview for {genre['name']}: {e}")
                continue

    except Exception as e:
        print(f"Error fetching preview movies: {e}")
        import traceback

        traceback.print_exc()
        solo_movies = []
        group_movies = []
        community_previews = []

    get_token(request)  # ensure CSRF cookie
    return render(
        request,
        "recom_sys_app/profile.html",
        {
            "profile": profile,
            "user_groups": user_groups,
            "created_groups": created_groups,
            "solo_movies": solo_movies,
            "group_movies": group_movies,
            "community_previews": community_previews,
        },
    )


@login_required
def communities_view(request):
    """
    Communities view - shows only COMMUNITY groups (genre-based browsing)
    URL: /communities/
    """
    profile, _ = UserProfile.objects.get_or_create(
        user=request.user, defaults={"name": request.user.username}
    )

    # Get user's COMMUNITY memberships only
    user_communities = (
        GroupMember.objects.filter(
            user=request.user,
            is_active=True,
            group_session__kind=GroupSession.Kind.COMMUNITY,
        )
        .select_related("group_session")
        .order_by("-joined_at")
    )

    # TMDB standard genre list
    genres = [
        {"id": 28, "name": "Action"},
        {"id": 12, "name": "Adventure"},
        {"id": 16, "name": "Animation"},
        {"id": 35, "name": "Comedy"},
        {"id": 80, "name": "Crime"},
        {"id": 18, "name": "Drama"},
        {"id": 10751, "name": "Family"},
        {"id": 14, "name": "Fantasy"},
        {"id": 36, "name": "History"},
        {"id": 27, "name": "Horror"},
        {"id": 10402, "name": "Music"},
        {"id": 9648, "name": "Mystery"},
        {"id": 10749, "name": "Romance"},
        {"id": 878, "name": "Science Fiction"},
        {"id": 53, "name": "Thriller"},
        {"id": 10752, "name": "War"},
        {"id": 37, "name": "Western"},
    ]

    get_token(request)  # ensure CSRF cookie
    return render(
        request,
        "recom_sys_app/communities.html",
        {
            "profile": profile,
            "user_communities": user_communities,
            "genres": genres,
        },
    )


# ============================================
# Edit Profile & Interactions
# ============================================


@login_required
@require_http_methods(["GET", "POST"])
def edit_profile_view(request):
    """
    Edit profile view for updating user preferences.
    GET: Display profile edit form
    POST: Update profile (including profile image)
    """
    profile, _ = UserProfile.objects.get_or_create(
        user=request.user, defaults={"name": request.user.username}
    )

    if request.method == "POST":
        form = UserProfileForm(request.POST, request.FILES, instance=profile)

        # Debug logging
        print(f"[Profile Edit] POST data keys: {list(request.POST.keys())}")
        print(f"[Profile Edit] FILES keys: {list(request.FILES.keys())}")
        print(
            f"[Profile Edit] Has profile_image in FILES: {'profile_image' in request.FILES}"
        )
        if "profile_image" in request.FILES:
            print(f"[Profile Edit] File name: {request.FILES['profile_image'].name}")
            print(f"[Profile Edit] File size: {request.FILES['profile_image'].size}")

        if form.is_valid():
            form.instance.user = request.user

            # Handle profile image removal
            if request.POST.get("profile_image-clear") == "on":
                # Delete the old image file if it exists
                if profile.profile_image:
                    try:
                        profile.profile_image.delete(save=False)
                    except Exception as e:
                        print(f"[Profile Edit] Error deleting old image: {e}")
                form.instance.profile_image = None
            # Handle new image upload - ensure it's saved
            elif "profile_image" in request.FILES:
                # The file is in request.FILES, form should handle it
                # But ensure it's properly assigned
                uploaded_file = request.FILES["profile_image"]
                form.instance.profile_image = uploaded_file
                print(f"[Profile Edit] Assigning uploaded file: {uploaded_file.name}")

            try:
                profile = form.save()
                print(f"[Profile Edit] Profile saved. Image: {profile.profile_image}")
                return redirect("recom_sys:profile")
            except Exception as e:
                print(f"[Profile Edit] Error saving profile: {e}")
                import traceback

                traceback.print_exc()
                # Re-render form with error
                return render(
                    request,
                    "recom_sys_app/edit_profile.html",
                    {"form": form, "error": f"Error saving profile: {str(e)}"},
                )
        else:
            # Log form errors for debugging
            print(f"[Profile Edit] Form errors: {form.errors}")
            print(f"[Profile Edit] Form non_field_errors: {form.non_field_errors()}")
    else:
        form = UserProfileForm(instance=profile)

    return render(request, "recom_sys_app/edit_profile.html", {"form": form})


@login_required
@require_http_methods(["POST"])
def set_interaction_view(request, tmdb_id: int, status: str):
    """
    Set or update a movie interaction (AJAX endpoint).
    Used by template-based frontend.
    """
    status = status.upper()
    valid = {c for c, _ in Interaction.Status.choices}

    if status not in valid:
        return JsonResponse(
            {"ok": False, "error": f"Invalid status {status}"}, status=400
        )

    obj, _created = Interaction.objects.update_or_create(
        user=request.user,
        tmdb_id=tmdb_id,
        defaults={"status": status, "source": "solo"},
    )

    return JsonResponse({"ok": True, "tmdb_id": tmdb_id, "status": obj.status})


# ============================================
# Recommendations
# ============================================


@login_required
def recommend_view(request):
    """
    Movie recommendation view for Solo mode.
    Generates personalized recommendations based on user history or onboarding preferences.
    Can optionally start with a specific movie if movie_id is provided.
    """
    try:
        # Check if a specific movie was selected from homepage
        start_movie_id = request.GET.get("movie_id")
        start_movie = None

        if start_movie_id:
            # Fetch details for the starting movie
            start_movies = _tmdb_fetch_by_ids([int(start_movie_id)])
            if start_movies and start_movies[0].get("found", False):
                start_movie = start_movies[0]

        # Use RecommendationService to get personalized solo deck
        movie_ids = RecommendationService.get_solo_deck(request.user, limit=50)

        # If we have a starting movie, get similar movies to it as well
        if start_movie_id:
            try:
                # Fetch similar movies to the selected movie
                similar_response = requests.get(
                    f"https://api.themoviedb.org/3/movie/{start_movie_id}/similar",
                    params={"api_key": settings.TMDB_API_KEY, "page": 1},
                    timeout=10,
                )
                if similar_response.status_code == 200:
                    similar_data = similar_response.json()
                    similar_ids = [
                        m["id"] for m in similar_data.get("results", [])[:20]
                    ]
                    # Add similar movies to the deck (deduplicated)
                    movie_ids = similar_ids + [
                        mid for mid in movie_ids if mid not in similar_ids
                    ]
            except Exception as e:
                print(f"Error fetching similar movies: {e}")

        # Fetch TMDB details for recommended movies
        tmdb_results = _tmdb_fetch_by_ids(movie_ids) if movie_ids else []

        # Filter to only successfully fetched movies
        tmdb_results = [m for m in tmdb_results if m.get("found", False)]

        # If we have a starting movie, put it at the beginning
        if start_movie:
            # Remove it from results if it exists to avoid duplicates
            tmdb_results = [
                m for m in tmdb_results if m.get("tmdb_id") != int(start_movie_id)
            ]
            # Add it at the beginning
            tmdb_results.insert(0, start_movie)

        context = {
            "agent_text": "",  # No AI agent text in new implementation
            "results": json.dumps(
                tmdb_results
            ),  # Convert to JSON string for JavaScript
            "user_movies": _get_signup_movies(request.user),
            "user_genres": _get_signup_genre(request.user),
            "start_movie_title": start_movie.get("title") if start_movie else None,
        }

        return render(request, "recom_sys_app/recommend_cards.html", context)

    except Exception as e:
        return JsonResponse(
            {"error": f"Recommendation error: {e.__class__.__name__}: {e}"}, status=500
        )


# ============================================
# Home / Landing Page
# ============================================


def home_view(request):
    """
    Landing page view.
    Shows login/signup for anonymous users, dashboard for authenticated users.
    """
    if request.user.is_authenticated:
        return redirect("recom_sys:profile")

    return render(request, "recom_sys_app/home.html")


def health_check(request):
    """
    Simple health check endpoint for AWS ELB.
    Returns 200 OK without requiring authentication.
    """
    return HttpResponse("OK", status=200)


# ============================================
# Additional Helper Views
# ============================================


@login_required
def user_stats_view(request):
    """
    Get user statistics for dashboard.
    Returns JSON with interaction counts and preferences.
    """
    try:
        profile = UserProfile.objects.get(user=request.user)
        interactions = Interaction.objects.filter(user=request.user)

        stats = {
            "profile": {
                "name": profile.name,
                "favourite_genres": [
                    g for g in [profile.favourite_genre1, profile.favourite_genre2] if g
                ],
                "onboarding_complete": profile.onboarding_complete,
            },
            "interactions": {
                "total": interactions.count(),
                "liked": interactions.filter(status="LIKE").count(),
                "disliked": interactions.filter(status="DISLIKE").count(),
                "watched": interactions.filter(status="WATCHED").count(),
                "watch_later": interactions.filter(status="WATCH_LATER").count(),
            },
            "preferences": {
                "movies": _get_signup_movies(request.user),
                "genres": _get_signup_genre(request.user),
            },
        }

        return JsonResponse({"success": True, "stats": stats})

    except UserProfile.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Profile not found"}, status=404
        )
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)
