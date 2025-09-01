from __future__ import annotations

import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone

User = get_user_model()


class PayoutStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSED = "processed", "Processed"
    FAILED = "failed", "Failed"

class DriverProfile(models.Model):
    class Vehicle(models.TextChoices):
        BIKE = "bike", "Bike"
        CAR = "car", "Car"
        VAN = "van", "Van"
        TRUCK = "truck", "Truck"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"
        SUSPENDED = "suspended", "Suspended"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="driver_profile")
    license_number = models.CharField(max_length=50)
    vehicle_type = models.CharField(max_length=20, choices=Vehicle.choices)
    vehicle_registration = models.CharField(max_length=50, blank=True, null=True)
    is_verified = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.INACTIVE)

    # Performance
    total_deliveries = models.PositiveIntegerField(default=0)
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=0.00)

    def __str__(self):
        return f"DriverProfile({self.user_id})"


class DriverDocument(models.Model):
    """KYC documents for drivers (license scan, insurance, national ID)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    driver = models.ForeignKey(DriverProfile, on_delete=models.CASCADE, related_name="documents")
    doc_type = models.CharField(max_length=50)  # e.g., license, insurance, id
    file = models.FileField(upload_to="drivers/docs/")
    uploaded_at = models.DateTimeField(default=timezone.now)
    verified = models.BooleanField(default=False)
    notes = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"DriverDocument({self.doc_type}, {self.driver_id})"


class DriverInvitation(models.Model):
    """Admin invites a driver; driver accepts and sets password via token."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField()
    full_name = models.CharField(max_length=255)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="driver_invites_created")
    token = models.UUIDField(default=uuid.uuid4, unique=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(blank=True, null=True)

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    def __str__(self):
        return f"DriverInvitation({self.email})"

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
