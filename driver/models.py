from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from users.models import DriverProfile


class PayoutStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSED = "processed", "Processed"
    FAILED = "failed", "Failed"


class DriverAvailability(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    driver_profile = models.OneToOneField(DriverProfile, on_delete=models.CASCADE, related_name="availability")
    available = models.BooleanField(default=False)
    lat = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    lng = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    last_updated = models.DateTimeField(default=timezone.now)


class DriverPayout(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    driver_profile = models.ForeignKey(DriverProfile, on_delete=models.CASCADE, related_name="payouts")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=16, choices=PayoutStatus.choices, default=PayoutStatus.PENDING)
    payout_date = models.DateTimeField(blank=True, null=True)
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)


class DriverRating(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    driver_profile = models.ForeignKey(DriverProfile, on_delete=models.CASCADE, related_name="ratings")
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="driver_ratings")
    # Optional link to booking (if bookings app is installed)
    booking = models.ForeignKey("bookings.Booking", on_delete=models.SET_NULL, null=True, blank=True)
    rating = models.PositiveSmallIntegerField()
    comment = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("driver_profile", "customer", "booking")
