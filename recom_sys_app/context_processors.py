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
    websocket_host = getattr(settings, "WEBSOCKET_HOST", "")
    production_domain = getattr(settings, "PRODUCTION_DOMAIN", "")
    cloudfront_domain = getattr(settings, "CLOUDFRONT_DOMAIN", "")
    use_https = getattr(settings, "USE_HTTPS", False)

    # For media files: if accessed via CloudFront, use EB domain
    # CloudFront doesn't serve /media/ files, so we need to use EB directly
    media_host = ""
    host = request.get_host()
    if cloudfront_domain and cloudfront_domain in host:
        # User is accessing via CloudFront - use EB domain for media
        if production_domain:
            media_host = production_domain
        elif websocket_host:
            media_host = websocket_host

    return {
        "WEBSOCKET_HOST": websocket_host,
        "USE_HTTPS": use_https,
        "MEDIA_HOST": media_host,
        "PRODUCTION_DOMAIN": production_domain,
    }
