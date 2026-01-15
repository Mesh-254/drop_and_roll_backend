from django.urls import path, include, re_path
from rest_framework.routers import DefaultRouter
from driver.consumers import TrackingConsumer

from driver.api_views import (
    DriverAvailabilityViewSet, DriverPayoutViewSet, DriverRatingViewSet, DriverDocumentViewSet, DriverInviteViewSet,
DriverAssignedBookingViewSet, DriverMetricsView, DriverRouteViewSet, DriverTrackingViewSet)

router = DefaultRouter()

router.register(r"availability", DriverAvailabilityViewSet, basename="driver-availability")
router.register(r"payouts", DriverPayoutViewSet, basename="driver-payouts")
router.register(r"ratings", DriverRatingViewSet, basename="driver-ratings")

router.register(r"driver-docs", DriverDocumentViewSet, basename="driver-docs")
router.register(r"driver-invites", DriverInviteViewSet, basename="driver-invites")

router.register(r'assigned-bookings', DriverAssignedBookingViewSet, basename='driver-assigned-booking')
router.register(r'driver-routes', DriverRouteViewSet, basename='driver-route')

router.register(r'live-tracking', DriverTrackingViewSet, basename='driver-tracking')


urlpatterns = [path("", include(router.urls)),
               path('driver-metrics/', DriverMetricsView.as_view(), name='driver-metrics'),
               path('driver-docs/<uuid:pk>/verify', DriverDocumentViewSet.as_view({'post': 'verify_document'}), name='driver-document-verify'),
               
               ]

websocket_urlpatterns = [
    re_path(r'ws/tracking/$', TrackingConsumer.as_asgi()),
]