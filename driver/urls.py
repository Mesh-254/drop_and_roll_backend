from django.urls import path, include
from rest_framework.routers import DefaultRouter

from driver.api_views import (
    DriverAvailabilityViewSet, DriverPayoutViewSet, DriverRatingViewSet, )

router = DefaultRouter()

router.register(r"availability", DriverAvailabilityViewSet, basename="driver-availability")
router.register(r"payouts", DriverPayoutViewSet, basename="driver-payouts")
router.register(r"ratings", DriverRatingViewSet, basename="driver-ratings")

urlpatterns = [path("", include(router.urls))]
