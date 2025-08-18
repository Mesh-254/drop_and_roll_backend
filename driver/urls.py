from django.urls import path, include
from rest_framework.routers import DefaultRouter
from driver.api_views import (
    DriverProfileViewSet, DriverDocumentViewSet, DriverDocumentModerationViewSet,
    DriverAvailabilityViewSet, DriverPayoutViewSet, DriverRatingViewSet, DriverInviteViewSet
)

router = DefaultRouter()
router.register(r"profiles", DriverProfileViewSet, basename="driver-profiles")
router.register(r"documents", DriverDocumentViewSet, basename="driver-documents")
router.register(r"documents-moderation", DriverDocumentModerationViewSet, basename="driver-docs-moderation")
router.register(r"availability", DriverAvailabilityViewSet, basename="driver-availability")
router.register(r"payouts", DriverPayoutViewSet, basename="driver-payouts")
router.register(r"ratings", DriverRatingViewSet, basename="driver-ratings")
router.register(r"invites", DriverInviteViewSet, basename="driver-invites")

urlpatterns = [path("", include(router.urls))]
