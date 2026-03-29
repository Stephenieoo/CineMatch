# recom_sys_app/views_group_chat.py
# Chat API views for group sessions (HTTP fallback + history)
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from .models import GroupSession, GroupMember, GroupChatMessage


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_chat_history(request, group_code):
    """
    Get chat history for a group.

    URL: GET /api/groups/<group_code>/chat/history/
    Query params:
        - limit: Number of messages (default: 50, max: 100)
        - before_id: Get messages before this ID (pagination)

    Example:
        GET /api/groups/ABC123/chat/history/?limit=50
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

        limit = min(int(request.GET.get("limit", 50)), 100)
        before_id = request.GET.get("before_id")

        messages = (
            GroupChatMessage.objects.filter(group_session=group_session)
            .select_related("user")
            .order_by("-created_at")
        )

        if before_id:
            messages = messages.filter(id__lt=int(before_id))

        messages_list = list(messages[: limit + 1])
        has_more = len(messages_list) > limit

        if has_more:
            messages_list = messages_list[:limit]

        messages_list.reverse()

        messages_data = [
            {
                "id": msg.id,
                "user": msg.user.username,
                "message": msg.content,
                "created_at": msg.created_at.isoformat(),
                "is_system_message": msg.is_system_message,
            }
            for msg in messages_list
        ]

        return Response(
            {
                "success": True,
                "group_code": group_code,
                "messages": messages_data,
                "count": len(messages_data),
                "has_more": has_more,
            },
            status=status.HTTP_200_OK,
        )

    except ValueError as e:
        return Response(
            {"error": f"Invalid parameter: {str(e)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        print(f"[ERROR] get_chat_history: {e}")
        import traceback

        traceback.print_exc()
        return Response(
            {"error": f"Server Error: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def send_chat_message(request, group_code):
    """
    Send a chat message via HTTP (alternative to WebSocket).

    URL: POST /api/groups/<group_code>/chat/send/
    Body:
    {
        "message": "Hello everyone!"
    }
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

        message_text = request.data.get("message", "").strip()

        if not message_text:
            return Response(
                {"error": "Message cannot be empty."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if len(message_text) > 500:
            return Response(
                {"error": "Message too long (max 500 characters)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        chat_message = GroupChatMessage.objects.create(
            group_session=group_session,
            user=request.user,
            content=message_text,
            is_system_message=False,
        )

        try:
            channel_layer = get_channel_layer()
            room_group_name = f"chat_{group_code}"
            async_to_sync(channel_layer.group_send)(
                room_group_name,
                {
                    "type": "chat_message",
                    "message_id": chat_message.id,
                    "message": message_text,
                    "user_id": request.user.id,
                    "username": request.user.username,
                    "timestamp": chat_message.created_at.isoformat(),
                },
            )
            print(f"[HTTP Chat] Message broadcast to WebSocket: {message_text[:50]}")
        except Exception as e:
            print(f"[WARNING] Failed to broadcast message via WebSocket: {e}")

        return Response(
            {
                "success": True,
                "message_id": chat_message.id,
                "created_at": chat_message.created_at.isoformat(),
            },
            status=status.HTTP_201_CREATED,
        )

    except Exception as e:
        print(f"[ERROR] send_chat_message: {e}")
        import traceback

        traceback.print_exc()
        return Response(
            {"error": f"Server Error: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
