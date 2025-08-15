from django.urls import path, include
from rest_framework.routers import DefaultRouter
from users.api_views import AuthViewSet, ProfileViewSet, DriverDocumentViewSet, DriverInviteViewSet

router = DefaultRouter()
router.register(r"auth", AuthViewSet, basename="auth")
router.register(r"profiles", ProfileViewSet, basename="profiles")
router.register(r"driver-docs", DriverDocumentViewSet, basename="driver-docs")
router.register(r"driver-invites", DriverInviteViewSet, basename="driver-invites")

urlpatterns = [
    path("", include(router.urls)),
]
