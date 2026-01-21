"""
ASGI config for Drop_N_Roll project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application
from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import WebsocketDenier

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "Drop_N_Roll.settings")

# This is just a lazy wrapper - real setup happens on first request
django_asgi_app = get_asgi_application()

from driver.urls import websocket_urlpatterns  # Import the websocket_urlpatterns

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AuthMiddlewareStack(
            URLRouter(websocket_urlpatterns)
        ),
    }
)
