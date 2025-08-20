from django.urls import path, include
from rest_framework.routers import DefaultRouter

from bookings.api_views import QuoteViewSet, BookingViewSet, ShippingTypeViewSet, ServiceTypeViewSet

router = DefaultRouter()
router.register(r"quotes", QuoteViewSet, basename="quotes")
router.register(r"bookings", BookingViewSet, basename="bookings")
router.register(r"shipping-types", ShippingTypeViewSet, basename="shipping-types")
router.register(r"service-types", ServiceTypeViewSet, basename="service-type")
urlpatterns = [
    path("", include(router.urls)),
]
