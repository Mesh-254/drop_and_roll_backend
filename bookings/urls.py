from django.urls import path, include
from rest_framework.routers import DefaultRouter
from bookings.api_views import QuoteViewSet, BookingViewSet

router = DefaultRouter()
router.register(r"quotes", QuoteViewSet, basename="quotes")
router.register(r"bookings", BookingViewSet, basename="bookings")

urlpatterns = [
    path("", include(router.urls)),
]