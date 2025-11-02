from django.urls import path, include
from rest_framework.routers import DefaultRouter
from users.api_views import AuthViewSet, ProfileViewSet,GoogleLoginView, login_to_admin

router = DefaultRouter()
router.register(r"auth", AuthViewSet, basename="auth")
router.register(r"profiles", ProfileViewSet, basename="profiles")

urlpatterns = [
    path("", include(router.urls)),
    path("auth/google/", GoogleLoginView.as_view(), name="google-login"),
    path("auth/admin/", login_to_admin, name="admin-login"),
]
