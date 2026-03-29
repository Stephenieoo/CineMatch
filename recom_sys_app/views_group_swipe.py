# recom_sys_app/views_group_swipe.py
# Group movie deck, swipe (like/dislike), matching, and completion logic
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404, render
from django.db import transaction
from django.contrib.auth.decorators import login_required
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from .models import GroupSession, GroupMember, GroupSwipe, GroupMatch
from .services import RecommendationService


# ==================== WebSocket Broadcast Helpers ====================


def _broadcast_completion_event(group_code, completion_status):
    """
    Broadcast a WebSocket event to notify all group members that everyone has finished swiping.
    """
    try:
        print(f"[_broadcast_completion_event] Broadcasting to group {group_code}")
        channel_layer = get_channel_layer()
        group_name = f"match_{group_code}"

        event_data = {
            "type": "all_members_finished",
            "total_members": completion_status["total_members"],
            "finished_members": completion_status["finished_members"],
            "total_movies": completion_status["total_movies"],
            "common_matches_count": completion_status.get("common_matches_count", 0),
            "message": "🎉 Everyone has finished swiping! Check out your common matches!",
        }

        async_to_sync(channel_layer.group_send)(group_name, event_data)
        print(f"[WebSocket] Broadcast completion event for group {group_code}")

    except Exception as e:
        print(f"[WebSocket] Error broadcasting completion event: {e}")


def _broadcast_match_event(
    group_code, match_id, tmdb_id, movie_title, movie_info, matched_by_users, matched_at
):
    """
    Broadcast a match_found event to all WebSocket clients in the group.
    """
    try:
        print(f"[_broadcast_match_event] Starting broadcast for group {group_code}")
        channel_layer = get_channel_layer()
        group_name = f"match_{group_code}"

        poster_url = None
        if movie_info and movie_info.get("poster_path"):
            poster_url = f"https://image.tmdb.org/t/p/w500{movie_info['poster_path']}"

        event_data = {
            "type": "match_found",
            "match_id": match_id,
            "tmdb_id": tmdb_id,
            "movie_title": movie_title,
            "poster_url": poster_url,
            "year": movie_info.get("release_date", "")[:4] if movie_info else None,
            "genres": movie_info.get("genres", []) if movie_info else [],
            "overview": movie_info.get("overview", "") if movie_info else "",
            "vote_average": movie_info.get("vote_average") if movie_info else None,
            "matched_at": matched_at,
            "matched_by": matched_by_users,
            "member_count": len(matched_by_users),
            "message": f'🎉 Match! Everyone likes "{movie_title}"!',
        }

        async_to_sync(channel_layer.group_send)(group_name, event_data)
        print(
            f"[WebSocket] Broadcast match event for group {group_code}, movie: {movie_title}"
        )

    except Exception as e:
        print(f"[WebSocket] Error broadcasting match event: {e}")


# ==================== Page Views ====================


@login_required
def group_room_view(request, group_code):
    """
    Group Room Page
    URL: /groups/<group_code>/room/
    """
    return render(request, "recom_sys_app/group_lobby.html", {"group_code": group_code})


@login_required
def group_deck_view(request, group_code):
    """
    Render Group Movie Recommendations Swipe Card Page
    URL: /groups/<group_code>/deck/
    """
    try:
        group_session = get_object_or_404(
            GroupSession, group_code=group_code, is_active=True
        )

        is_member = GroupMember.objects.filter(
            group_session=group_session, user=request.user, is_active=True
        ).exists()

        if not is_member:
            return render(
                request,
                "recom_sys_app/error.html",
                {
                    "error_message": "You are not a member of this group.",
                    "group_code": group_code,
                },
            )

        member_count = GroupMember.objects.filter(
            group_session=group_session, is_active=True
        ).count()

        context = {
            "group_code": group_code,
            "group_session": group_session,
            "member_count": member_count,
            "user": request.user,
            "is_community": group_session.kind == GroupSession.Kind.COMMUNITY,
        }

        return render(request, "recom_sys_app/group_deck.html", context)

    except Exception as e:
        return render(
            request,
            "recom_sys_app/error.html",
            {"error_message": str(e), "group_code": group_code},
        )


# ==================== API Views ====================


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_group_deck(request, group_code):
    """
    Retrieve the movie recommendation list for a group.
    URL: GET /api/groups/<group_code>/deck/
    """
    try:
        print(f"[DEBUG] request.GET = {request.GET}")
        print(f"[DEBUG] group_code = {group_code}")
        print(f"[DEBUG] user = {request.user}")

        group_session = get_object_or_404(
            GroupSession, group_code=group_code, is_active=True
        )

        is_member = GroupMember.objects.filter(
            group_session=group_session, user=request.user, is_active=True
        ).exists()

        if not is_member:
            return Response(
                {"error": "You are not a member of this group."},
                status=status.HTTP_403_FORBIDDEN,
            )

        limit = int(request.GET.get("limit", 20))
        with_details = request.GET.get("with_details", "false").lower() == "true"
        limit = min(max(limit, 1), 100)

        movie_ids = RecommendationService.get_group_deck(group_session, limit=limit)

        if with_details:
            movies = []
            for tmdb_id in movie_ids:
                movie_info = RecommendationService.get_movie_details(tmdb_id)
                if movie_info:
                    movies.append(movie_info)
        else:
            movies = [{"tmdb_id": mid} for mid in movie_ids]

        member_count = GroupMember.objects.filter(
            group_session=group_session, is_active=True
        ).count()

        return Response(
            {
                "group_code": group_session.group_code,
                "member_count": member_count,
                "movies": movies,
                "total": len(movies),
                "message": "Movie list retrieved successfully",
            },
            status=status.HTTP_200_OK,
        )

    except ValueError as e:
        return Response(
            {"error": f"Invalid parameter format: {str(e)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        import traceback

        traceback.print_exc()
        return Response(
            {"error": f"Server Error: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def swipe_like(request, group_code):
    """
    Record user Like for a movie (right swipe).

    URL: POST /api/groups/<group_code>/swipe/like/
    Body: { "tmdb_id": 12345, "movie_title": "Fight Club" }
    Response: { "success": true, "swipe_id": 123, "action": "LIKE",
                "is_match": true, "match_data": {...} }
    """
    print(
        f"[DEBUG swipe_like] Called for group {group_code} by user {request.user.username}"
    )
    try:
        group_session = get_object_or_404(
            GroupSession, group_code=group_code, is_active=True
        )

        is_member = GroupMember.objects.filter(
            group_session=group_session, user=request.user, is_active=True
        ).exists()

        if not is_member:
            return Response(
                {"error": "You are not a member of this group."},
                status=status.HTTP_403_FORBIDDEN,
            )

        tmdb_id = request.data.get("tmdb_id")
        movie_title = request.data.get("movie_title", "")
        detailed_action = request.data.get("detailed_action", "LIKE")

        if not tmdb_id:
            return Response(
                {"error": "The tmdb_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Community groups use Interaction model (no matching logic)
        if group_session.kind == GroupSession.Kind.COMMUNITY:
            from .models import Interaction

            existing_interaction = Interaction.objects.filter(
                user=request.user, tmdb_id=tmdb_id
            ).first()

            if existing_interaction:
                existing_interaction.status = Interaction.Status.LIKE
                existing_interaction.save()
                return Response(
                    {
                        "success": True,
                        "interaction_id": existing_interaction.id,
                        "action": "LIKE",
                        "tmdb_id": tmdb_id,
                        "is_match": False,
                        "match_data": None,
                        "timestamp": existing_interaction.updated_at.isoformat(),
                    },
                    status=status.HTTP_200_OK,
                )
            else:
                interaction = Interaction.objects.create(
                    user=request.user,
                    tmdb_id=tmdb_id,
                    status=Interaction.Status.LIKE,
                    source="community",
                )
                return Response(
                    {
                        "success": True,
                        "interaction_id": interaction.id,
                        "action": "LIKE",
                        "tmdb_id": tmdb_id,
                        "is_match": False,
                        "match_data": None,
                        "timestamp": interaction.created_at.isoformat(),
                    },
                    status=status.HTTP_201_CREATED,
                )

        # Private groups: use GroupSwipe + Interaction for consistency
        from .models import Interaction

        interaction_status = Interaction.Status.LIKE
        if detailed_action == "WATCHED_LIKED":
            interaction_status = Interaction.Status.WATCHED_LIKED
        elif detailed_action == "WATCHED_DISLIKED":
            interaction_status = Interaction.Status.WATCHED_DISLIKED

        existing_swipe = GroupSwipe.objects.filter(
            group_session=group_session, user=request.user, tmdb_id=tmdb_id
        ).first()

        response_status = status.HTTP_200_OK
        message = None

        with transaction.atomic():
            # Keep Interaction model in sync
            existing_interaction = Interaction.objects.filter(
                user=request.user, tmdb_id=tmdb_id
            ).first()
            if existing_interaction:
                existing_interaction.status = interaction_status
                existing_interaction.source = "group"
                existing_interaction.save()
            else:
                Interaction.objects.create(
                    user=request.user,
                    tmdb_id=tmdb_id,
                    status=interaction_status,
                    source="group",
                )

            # Handle GroupSwipe for matching logic
            if existing_swipe:
                if existing_swipe.action == GroupSwipe.Action.LIKE:
                    return Response(
                        {
                            "success": True,
                            "swipe_id": existing_swipe.id,
                            "action": existing_swipe.action,
                            "tmdb_id": tmdb_id,
                            "is_match": False,
                            "match_data": None,
                            "message": "Already liked this movie",
                            "timestamp": existing_swipe.created_at.isoformat(),
                        },
                        status=status.HTTP_200_OK,
                    )
                else:
                    existing_swipe.action = GroupSwipe.Action.LIKE
                    existing_swipe.save()
                    swipe = existing_swipe
                    message = "Updated to LIKE"
            else:
                swipe = GroupSwipe.objects.create(
                    group_session=group_session,
                    user=request.user,
                    tmdb_id=tmdb_id,
                    action=GroupSwipe.Action.LIKE,
                )
                response_status = status.HTTP_201_CREATED

            # Check if all members matched on this movie
            is_match = RecommendationService.check_group_match(group_session, tmdb_id)

            match_data = None
            if is_match:
                try:
                    match, created = GroupMatch.objects.get_or_create(
                        group_session=group_session,
                        tmdb_id=tmdb_id,
                        defaults={"movie_title": movie_title},
                    )
                    movie_info = RecommendationService.get_movie_details(tmdb_id)
                    matched_by_users = list(
                        GroupSwipe.objects.filter(
                            group_session=group_session,
                            tmdb_id=tmdb_id,
                            action=GroupSwipe.Action.LIKE,
                        ).values_list("user__username", flat=True)
                    )

                    poster_url = None
                    if movie_info and movie_info.get("poster_path"):
                        poster_url = (
                            f"https://image.tmdb.org/t/p/w500{movie_info['poster_path']}"
                        )

                    genres_list = []
                    if movie_info and movie_info.get("genres"):
                        genres = movie_info.get("genres", [])
                        if genres and isinstance(genres[0], dict):
                            genres_list = [g.get("name", str(g)) for g in genres]
                        elif genres and isinstance(genres[0], str):
                            genres_list = genres

                    resolved_title = movie_title or (
                        movie_info.get("title") if movie_info else "this movie"
                    )
                    match_data = {
                        "match_id": match.id,
                        "tmdb_id": tmdb_id,
                        "movie_title": resolved_title,
                        "poster_url": poster_url,
                        "year": (
                            movie_info.get("release_date", "")[:4]
                            if movie_info and movie_info.get("release_date")
                            else None
                        ),
                        "genres": genres_list,
                        "overview": movie_info.get("overview", "") if movie_info else "",
                        "vote_average": (
                            movie_info.get("vote_average") if movie_info else None
                        ),
                        "matched_at": match.matched_at.isoformat(),
                        "matched_by": matched_by_users,
                        "member_count": len(matched_by_users),
                        "message": f"[MATCH] Everyone likes '{resolved_title}'!",
                    }

                    if created:
                        _broadcast_match_event(
                            group_session.group_code,
                            match.id,
                            tmdb_id,
                            resolved_title,
                            movie_info,
                            matched_by_users,
                            match.matched_at.isoformat(),
                        )

                except Exception as e:
                    import traceback

                    traceback.print_exc()
                    raise

            RecommendationService.invalidate_deck_cache(group_session)

        response_data = {
            "success": True,
            "swipe_id": swipe.id,
            "action": swipe.action,
            "tmdb_id": tmdb_id,
            "is_match": is_match,
            "match_data": match_data,
            "timestamp": swipe.created_at.isoformat(),
        }
        if message:
            response_data["message"] = message

        completion_status = RecommendationService.check_all_members_finished(
            group_session
        )
        if completion_status["all_finished"]:
            common_matches = RecommendationService.get_all_common_matches(group_session)
            completion_status["common_matches_count"] = len(common_matches)
            _broadcast_completion_event(group_session.group_code, completion_status)

        return Response(response_data, status=response_status)

    except Exception as e:
        import traceback

        traceback.print_exc()
        return Response(
            {"error": f"Server Error: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def swipe_dislike(request, group_code):
    """
    Record user Dislike for a movie (left swipe).

    URL: POST /api/groups/<group_code>/swipe/dislike/
    Body: { "tmdb_id": 550, "movie_title": "Fight Club" }
    Response: { "success": true, "swipe_id": 123, "action": "DISLIKE", "tmdb_id": 550 }
    """
    print(
        f"[DEBUG swipe_dislike] Called for group {group_code} by user {request.user.username}"
    )
    try:
        group_session = get_object_or_404(
            GroupSession, group_code=group_code, is_active=True
        )

        is_member = GroupMember.objects.filter(
            group_session=group_session, user=request.user, is_active=True
        ).exists()

        if not is_member:
            return Response(
                {"error": "You are not a member of this group."},
                status=status.HTTP_403_FORBIDDEN,
            )

        tmdb_id = request.data.get("tmdb_id")
        detailed_action = request.data.get("detailed_action", "DISLIKE")

        if not tmdb_id:
            return Response(
                {"error": "The tmdb_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Community groups use Interaction model
        if group_session.kind == GroupSession.Kind.COMMUNITY:
            from .models import Interaction

            existing_interaction = Interaction.objects.filter(
                user=request.user, tmdb_id=tmdb_id
            ).first()

            if existing_interaction:
                existing_interaction.status = Interaction.Status.DISLIKE
                existing_interaction.save()
                return Response(
                    {
                        "success": True,
                        "interaction_id": existing_interaction.id,
                        "action": "DISLIKE",
                        "tmdb_id": tmdb_id,
                        "timestamp": existing_interaction.updated_at.isoformat(),
                    },
                    status=status.HTTP_200_OK,
                )
            else:
                interaction = Interaction.objects.create(
                    user=request.user,
                    tmdb_id=tmdb_id,
                    status=Interaction.Status.DISLIKE,
                    source="community",
                )
                return Response(
                    {
                        "success": True,
                        "interaction_id": interaction.id,
                        "action": "DISLIKE",
                        "tmdb_id": tmdb_id,
                        "timestamp": interaction.created_at.isoformat(),
                    },
                    status=status.HTTP_201_CREATED,
                )

        # Private groups
        from .models import Interaction

        interaction_status = Interaction.Status.DISLIKE
        if detailed_action == "WATCHED_DISLIKED":
            interaction_status = Interaction.Status.WATCHED_DISLIKED

        existing_swipe = GroupSwipe.objects.filter(
            group_session=group_session, user=request.user, tmdb_id=tmdb_id
        ).first()

        response_status = status.HTTP_200_OK
        message = None

        with transaction.atomic():
            # Keep Interaction model in sync
            existing_interaction = Interaction.objects.filter(
                user=request.user, tmdb_id=tmdb_id
            ).first()
            if existing_interaction:
                existing_interaction.status = interaction_status
                existing_interaction.source = "group"
                existing_interaction.save()
            else:
                Interaction.objects.create(
                    user=request.user,
                    tmdb_id=tmdb_id,
                    status=interaction_status,
                    source="group",
                )

            if existing_swipe:
                if existing_swipe.action == GroupSwipe.Action.DISLIKE:
                    return Response(
                        {
                            "success": True,
                            "swipe_id": existing_swipe.id,
                            "action": existing_swipe.action,
                            "tmdb_id": tmdb_id,
                            "message": "Already disliked this movie",
                            "timestamp": existing_swipe.created_at.isoformat(),
                        },
                        status=status.HTTP_200_OK,
                    )
                else:
                    existing_swipe.action = GroupSwipe.Action.DISLIKE
                    existing_swipe.save()
                    swipe = existing_swipe
                    message = "Updated to DISLIKE"
            else:
                swipe = GroupSwipe.objects.create(
                    group_session=group_session,
                    user=request.user,
                    tmdb_id=tmdb_id,
                    action=GroupSwipe.Action.DISLIKE,
                )
                response_status = status.HTTP_201_CREATED

            RecommendationService.invalidate_deck_cache(group_session)

        response_data = {
            "success": True,
            "swipe_id": swipe.id,
            "action": swipe.action,
            "tmdb_id": tmdb_id,
            "timestamp": swipe.created_at.isoformat(),
        }
        if message:
            response_data["message"] = message

        completion_status = RecommendationService.check_all_members_finished(
            group_session
        )
        if completion_status["all_finished"]:
            common_matches = RecommendationService.get_all_common_matches(group_session)
            completion_status["common_matches_count"] = len(common_matches)
            _broadcast_completion_event(group_session.group_code, completion_status)

        return Response(response_data, status=response_status)

    except Exception as e:
        import traceback

        traceback.print_exc()
        return Response(
            {"error": f"Server Error: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_group_matches(request, group_code):
    """
    Retrieve all matching records for a group.
    URL: GET /api/groups/<group_code>/matches/
    """
    try:
        group_session = get_object_or_404(
            GroupSession, group_code=group_code, is_active=True
        )

        is_member = GroupMember.objects.filter(
            group_session=group_session, user=request.user, is_active=True
        ).exists()

        if not is_member:
            return Response(
                {"error": "You are not a member of this group."},
                status=status.HTTP_403_FORBIDDEN,
            )

        matches = GroupMatch.objects.filter(group_session=group_session).order_by(
            "-matched_at"
        )

        matches_data = [
            {
                "match_id": match.id,
                "tmdb_id": match.tmdb_id,
                "movie_title": match.movie_title,
                "matched_at": match.matched_at.isoformat(),
            }
            for match in matches
        ]

        return Response(
            {
                "group_code": group_code,
                "matches": matches_data,
                "total": len(matches_data),
            },
            status=status.HTTP_200_OK,
        )

    except Exception as e:
        return Response(
            {"error": f"Server Error: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def join_or_create_community_group(request):
    """
    Join or create a public community group based on genre.
    URL: POST /api/groups/community/join/
    Body: { "genre_id": "28" }
    """
    try:
        from django.db import models as db_models

        genre_id = request.data.get("genre_id")

        if not genre_id:
            return Response(
                {"error": "Genre ID is required"}, status=status.HTTP_400_BAD_REQUEST
            )

        user = request.user

        existing_group = (
            GroupSession.objects.filter(
                is_public=True, is_active=True, genre_filter=genre_id
            )
            .annotate(member_count=db_models.Count("members"))
            .filter(member_count__lt=10)
            .order_by("-created_at")
            .first()
        )

        if existing_group:
            existing_member = GroupMember.objects.filter(
                group_session=existing_group, user=user
            ).first()

            if existing_member:
                return Response(
                    {
                        "success": True,
                        "action": "already_member",
                        "group_id": str(existing_group.id),
                        "group_code": existing_group.group_code,
                        "redirect_url": f"/group/{existing_group.id}/",
                    }
                )
            else:
                GroupMember.objects.create(
                    group_session=existing_group,
                    user=user,
                    role=GroupMember.Role.MEMBER,
                    is_active=True,
                )
                return Response(
                    {
                        "success": True,
                        "action": "joined",
                        "group_id": str(existing_group.id),
                        "group_code": existing_group.group_code,
                        "redirect_url": f"/group/{existing_group.id}/",
                    }
                )
        else:
            new_group = GroupSession.objects.create(
                creator=user, is_public=True, is_active=True, genre_filter=genre_id
            )
            return Response(
                {
                    "success": True,
                    "action": "created",
                    "group_id": str(new_group.id),
                    "group_code": new_group.group_code,
                    "redirect_url": f"/group/{new_group.id}/",
                }
            )

    except Exception as e:
        return Response(
            {"error": f"Server Error: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def check_completion_status(request, group_code):
    """
    Check the completion status of a group session.
    URL: GET /api/groups/<group_code>/completion-status/
    """
    try:
        group_session = get_object_or_404(
            GroupSession, group_code=group_code, is_active=True
        )

        is_member = GroupMember.objects.filter(
            group_session=group_session, user=request.user, is_active=True
        ).exists()

        if not is_member:
            return Response(
                {"error": "You are not a member of this group."},
                status=status.HTTP_403_FORBIDDEN,
            )

        completion_status = RecommendationService.check_all_members_finished(
            group_session
        )

        if completion_status["all_finished"]:
            common_matches = RecommendationService.get_all_common_matches(group_session)
            completion_status["common_matches_count"] = len(common_matches)
        else:
            completion_status["common_matches_count"] = 0

        return Response(completion_status, status=status.HTTP_200_OK)

    except Exception as e:
        return Response(
            {"error": f"Server Error: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_final_matches(request, group_code):
    """
    Retrieve the final list of movies that all active group members liked.
    URL: GET /api/groups/<group_code>/final-matches/
    """
    try:
        group_session = get_object_or_404(
            GroupSession, group_code=group_code, is_active=True
        )

        is_member = GroupMember.objects.filter(
            group_session=group_session, user=request.user, is_active=True
        ).exists()

        if not is_member:
            return Response(
                {"error": "You are not a member of this group."},
                status=status.HTTP_403_FORBIDDEN,
            )

        completion_status = RecommendationService.check_all_members_finished(
            group_session
        )
        common_matches = RecommendationService.get_all_common_matches(group_session)

        formatted_matches = []
        for match in common_matches:
            movie_info = match.get("movie_info", {})
            formatted_matches.append(
                {
                    "tmdb_id": match["tmdb_id"],
                    "movie_title": match["movie_title"],
                    "poster_url": (
                        f"https://image.tmdb.org/t/p/w500{movie_info.get('poster_path')}"
                        if movie_info.get("poster_path")
                        else None
                    ),
                    "year": (
                        movie_info.get("release_date", "")[:4]
                        if movie_info.get("release_date")
                        else None
                    ),
                    "genres": movie_info.get("genres", []),
                    "overview": movie_info.get("overview", ""),
                    "vote_average": movie_info.get("vote_average"),
                }
            )

        return Response(
            {
                "group_code": group_code,
                "all_finished": completion_status["all_finished"],
                "total_members": completion_status["total_members"],
                "finished_members": completion_status["finished_members"],
                "common_matches": formatted_matches,
                "total": len(formatted_matches),
            },
            status=status.HTTP_200_OK,
        )

    except Exception as e:
        return Response(
            {"error": f"Server Error: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def clear_group_swipes(request, group_code):
    """
    Clear all swipe records for a group (start a new round).
    URL: POST /api/groups/<group_code>/clear-swipes/
    """
    try:
        group_session = get_object_or_404(
            GroupSession, group_code=group_code, is_active=True
        )

        is_member = GroupMember.objects.filter(
            group_session=group_session, user=request.user, is_active=True
        ).exists()

        if not is_member:
            return Response(
                {"error": "You are not a member of this group."},
                status=status.HTTP_403_FORBIDDEN,
            )

        deleted_count = RecommendationService.clear_group_swipes(group_session)

        return Response(
            {
                "success": True,
                "deleted_count": deleted_count,
                "message": "Ready for new round!",
            },
            status=status.HTTP_200_OK,
        )

    except Exception as e:
        print(f"[ERROR clear_group_swipes] {e}")
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
