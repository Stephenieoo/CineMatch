"""
Context processors for recom_sys_app.
These make variables available to all templates.
"""

from django.conf import settings


def websocket_settings(request):
    """
    Provides WebSocket configuration to templates.
    
    If WEBSOCKET_HOST is set, WebSockets will connect directly to that host
    (useful when CloudFront doesn't proxy WebSockets properly).
    Otherwise, WebSockets connect to the same host as the page.
    """
    websocket_host = getattr(settings, 'WEBSOCKET_HOST', '')
    
    return {
        'WEBSOCKET_HOST': websocket_host,
        'USE_HTTPS': getattr(settings, 'USE_HTTPS', False),
    }



