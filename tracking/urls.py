from django.urls import path, include
from rest_framework.routers import DefaultRouter
from tracking.api_views import (
    TrackingSessionViewSet,
    DriverLocationViewSet,
    ProofOfDeliveryViewSet,
    GeofenceViewSet,
    WebhookSubscriptionViewSet,
)

router = DefaultRouter()
router.register(r"sessions", TrackingSessionViewSet, basename="tracking-sessions")
router.register(r"driver-locations", DriverLocationViewSet, basename="driver-locations")
router.register(r"pod", ProofOfDeliveryViewSet, basename="pod")
router.register(r"geofences", GeofenceViewSet, basename="geofences")
router.register(r"webhooks", WebhookSubscriptionViewSet, basename="tracking-webhooks")

urlpatterns = [
    path("", include(router.urls)),
]