from django.urls import path, include
from rest_framework.routers import DefaultRouter

from bookings.api_views import (
    BookingQRCodeView,
    QuoteViewSet,
    BookingViewSet,
    RouteViewSet,
    ShippingTypeViewSet,
    ServiceTypeViewSet,
    BookingStatusView,
    booking_qr_code,
    regenerate_qr,
    scan_qr,
    track_parcel,
)
from bookings.api_views import (
    QuoteViewSet,
    BookingViewSet,
    ShippingTypeViewSet,
    ServiceTypeViewSet,
    BookingStatusView,
    track_parcel,
)

router = DefaultRouter()
router.register(r"quotes", QuoteViewSet, basename="quotes")
router.register(r"bookings", BookingViewSet, basename="bookings")
router.register(r"shipping-types", ShippingTypeViewSet, basename="shipping-types")
router.register(r"service-types", ServiceTypeViewSet, basename="service-type")
router.register(r"routes", RouteViewSet, basename="routes")
urlpatterns = [
    path("", include(router.urls)),
    path("booking-statuses/", BookingStatusView.as_view(), name="booking-statuses"),
    path("track/", track_parcel, name="track-parcel"),
    
    path("scan-qr/", scan_qr, name="scan-qr"),
    path("bookings/<uuid:pk>/regenerate-qr/", regenerate_qr, name="regenerate-qr"),
    path("bookings/<uuid:pk>/qr/", booking_qr_code, name="booking-qr-code"),
]
