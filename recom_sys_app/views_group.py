# recom_sys_app/views_group.py
# Group session CRUD: create, join, details, lobby, leave, delete
import json
from django.db import transaction
from django.shortcuts import get_object_or_404, render, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_POST, require_http_methods
from django.contrib.auth.decorators import login_required

from .models import GroupSession, GroupMember


@login_required
@require_http_methods(["POST"])
def create_group(request):
    """
    Create a new group session.
    POST /api/groups
    """
    try:
        with transaction.atomic():
            group_code = GroupSession.generate_unique_code()
            group_session = GroupSession.objects.create(
                group_code=group_code, creator=request.user
            )
            GroupMember.objects.create(
                group_session=group_session,
                user=request.user,
                role=GroupMember.Role.CREATOR,
            )

            return JsonResponse(
                {
                    "success": True,
                    "message": "Group created successfully",
                    "data": {
                        "groupId": str(group_session.id),
                        "groupCode": group_session.group_code,
                        "createdAt": group_session.created_at.isoformat(),
                        "redirectUrl": f"/group/{group_session.id}/",
                    },
                },
                status=201,
            )

    except Exception as e:
        return JsonResponse(
            {"success": False, "message": f"Failed to create group: {str(e)}"},
            status=500,
        )


@login_required
@require_http_methods(["GET"])
def get_group_details(request, group_id):
    """
    Get group details.
    GET /api/groups/<group_id>
    """
    try:
        group = GroupSession.objects.get(id=group_id, is_active=True)

        is_member = GroupMember.objects.filter(
            group_session=group, user=request.user, is_active=True
        ).exists()

        if not is_member:
            return JsonResponse(
                {"success": False, "message": "You are not a member of this group"},
                status=403,
            )

        members = GroupMember.objects.filter(
            group_session=group, is_active=True
        ).select_related("user", "user__profile")

        members_data = []
        for member in members:
            member_info = {
                "username": member.user.username,
                "role": member.role,
                "joinedAt": member.joined_at.isoformat(),
            }
            if hasattr(member.user, "profile"):
                member_info["name"] = member.user.profile.name
            members_data.append(member_info)

        return JsonResponse(
            {
                "success": True,
                "data": {
                    "groupId": str(group.id),
                    "groupCode": group.group_code,
                    "creator": group.creator.username,
                    "createdAt": group.created_at.isoformat(),
                    "members": members_data,
                    "memberCount": len(members_data),
                },
            }
        )

    except GroupSession.DoesNotExist:
        return JsonResponse(
            {"success": False, "message": "Group not found"}, status=404
        )
    except Exception as e:
        return JsonResponse({"success": False, "message": str(e)}, status=500)


@login_required
def group_lobby(request, group_id):
    """Group lobby page. URL: /group/<uuid:group_id>/"""
    try:
        group = get_object_or_404(GroupSession, id=group_id, is_active=True)

        membership = GroupMember.objects.filter(
            group_session=group, user=request.user, is_active=True
        ).first()

        if not membership:
            return redirect("recom_sys:profile")

        context = {
            "group": group,
            "group_code": group.group_code,
            "is_creator": membership.role == GroupMember.Role.CREATOR,
            "is_community": group.kind == GroupSession.Kind.COMMUNITY,
        }

        return render(request, "recom_sys_app/group_lobby.html", context)

    except GroupSession.DoesNotExist:
        return redirect("recom_sys:profile")


@login_required
@require_http_methods(["POST"])
def join_group(request):
    """
    Join a group via group code.
    POST /api/groups/join
    Body: {"groupCode": "ABC123"}
    """
    try:
        data = json.loads(request.body)
        group_code = data.get("groupCode", "").strip().upper()

        if not group_code:
            return JsonResponse(
                {"success": False, "message": "Group code is required"}, status=400
            )

        try:
            group = GroupSession.objects.get(group_code=group_code, is_active=True)
        except GroupSession.DoesNotExist:
            return JsonResponse(
                {
                    "success": False,
                    "message": "Invalid group code. Please check and try again.",
                },
                status=404,
            )

        existing_member = GroupMember.objects.filter(
            group_session=group, user=request.user
        ).first()

        if existing_member:
            if existing_member.is_active:
                return JsonResponse(
                    {
                        "success": True,
                        "message": "You are already a member of this group",
                        "data": {
                            "groupId": str(group.id),
                            "groupCode": group.group_code,
                            "alreadyMember": True,
                            "redirectUrl": f"/group/{group.id}/",
                        },
                    }
                )
            else:
                existing_member.is_active = True
                existing_member.save()
                return JsonResponse(
                    {
                        "success": True,
                        "message": "Rejoined group successfully",
                        "data": {
                            "groupId": str(group.id),
                            "groupCode": group.group_code,
                            "rejoined": True,
                            "redirectUrl": f"/group/{group.id}/",
                        },
                    }
                )

        with transaction.atomic():
            GroupMember.objects.create(
                group_session=group,
                user=request.user,
                role=GroupMember.Role.MEMBER,
                is_active=True,
            )

        return JsonResponse(
            {
                "success": True,
                "message": "Joined group successfully",
                "data": {
                    "groupId": str(group.id),
                    "groupCode": group.group_code,
                    "creator": group.creator.username,
                    "redirectUrl": f"/group/{group.id}/",
                },
            },
            status=201,
        )

    except json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "message": "Invalid JSON data"}, status=400
        )
    except Exception as e:
        return JsonResponse(
            {"success": False, "message": f"Failed to join group: {str(e)}"}, status=500
        )


@login_required
@require_POST
def leave_group(request, group_id):
    """
    Leave a group (sets is_active=False for the member).
    POST /api/groups/<uuid:group_id>/leave/
    """
    try:
        group = get_object_or_404(GroupSession, id=group_id)

        membership = GroupMember.objects.filter(
            group_session=group, user=request.user, is_active=True
        ).first()

        if not membership:
            return JsonResponse(
                {"success": False, "message": "You are not a member of this group"},
                status=404,
            )

        if membership.role == GroupMember.Role.CREATOR:
            return JsonResponse(
                {
                    "success": False,
                    "message": "As the creator, you cannot leave. You can delete the group instead.",
                },
                status=400,
            )

        membership.is_active = False
        membership.save()

        return JsonResponse(
            {"success": True, "message": "You have left the group successfully"}
        )

    except Exception as e:
        return JsonResponse(
            {"success": False, "message": f"Failed to leave group: {str(e)}"},
            status=500,
        )


@login_required
@require_POST
def delete_group(request, group_id):
    """
    Delete a group (creator only).
    POST /api/groups/<uuid:group_id>/delete/
    """
    try:
        group = get_object_or_404(GroupSession, id=group_id)

        if group.creator != request.user:
            return JsonResponse(
                {
                    "success": False,
                    "message": "Only the group creator can delete this group",
                },
                status=403,
            )

        group_code = group.group_code
        group.delete()

        return JsonResponse(
            {
                "success": True,
                "message": f"Group {group_code} has been deleted successfully",
            }
        )

    except Exception as e:
        return JsonResponse(
            {"success": False, "message": f"Failed to delete group: {str(e)}"},
            status=500,
        )
