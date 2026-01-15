from django.urls import path, include
from rest_framework.routers import DefaultRouter

from bookings.api_views import QuoteViewSet, BookingViewSet, RouteViewSet, ShippingTypeViewSet, ServiceTypeViewSet, BookingStatusView, track_parcel
from bookings.api_views import QuoteViewSet, BookingViewSet, ShippingTypeViewSet, ServiceTypeViewSet, BookingStatusView, \
    track_parcel, booking_qr_code

router = DefaultRouter()
router.register(r"quotes", QuoteViewSet, basename="quotes")
router.register(r"bookings", BookingViewSet, basename="bookings")
router.register(r"shipping-types", ShippingTypeViewSet, basename="shipping-types")
router.register(r"service-types", ServiceTypeViewSet, basename="service-type")
router.register(r'routes', RouteViewSet, basename='routes')
urlpatterns = [
    path("", include(router.urls)),
    path("booking-statuses/", BookingStatusView.as_view(), name="booking-statuses"),
    path("track/", track_parcel, name="track-parcel"),
    path('bookings/<uuid:pk>/qr/', booking_qr_code, name='booking-qr-code'),
]
