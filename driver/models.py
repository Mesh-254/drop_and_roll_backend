from __future__ import annotations
import uuid
from decimal import Decimal
from django.conf import settings
from django.db import models
from django.utils import timezone


class DriverStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    INACTIVE = "inactive", "Inactive"
    SUSPENDED = "suspended", "Suspended"


class DocumentType(models.TextChoices):
    LICENSE = "license", "Driver License"
    NATIONAL_ID = "national_id", "National ID"
    INSURANCE = "insurance", "Insurance"
    VEHICLE_LOGBOOK = "vehicle_logbook", "Vehicle Logbook"
    OTHER = "other", "Other"


class DocumentStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class PayoutStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSED = "processed", "Processed"
    FAILED = "failed", "Failed"


class InviteStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    EXPIRED = "expired", "Expired"


class DriverProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="driver_profile_driver")

    vehicle_type = models.CharField(max_length=32, blank=True, default="")  # car/bike/van/truck
    license_number = models.CharField(max_length=64, blank=True, default="")

    is_verified = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=DriverStatus.choices, default=DriverStatus.INACTIVE)

    rating_avg = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("0.00"))
    rating_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"DriverProfile({self.user_id})"


class DriverDocument(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    driver_profile = models.ForeignKey(DriverProfile, on_delete=models.CASCADE, related_name="documents")
    document_type = models.CharField(max_length=32, choices=DocumentType.choices, default=DocumentType.LICENSE)
    file = models.FileField(upload_to="drivers/docs/")
    status = models.CharField(max_length=16, choices=DocumentStatus.choices, default=DocumentStatus.PENDING)
    reason = models.CharField(max_length=255, blank=True, default="")
    uploaded_at = models.DateTimeField(default=timezone.now)
    reviewed_at = models.DateTimeField(blank=True, null=True)


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


class DriverInvite(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField()
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="driver_invites")
    token = models.UUIDField(default=uuid.uuid4, unique=True)
    status = models.CharField(max_length=16, choices=InviteStatus.choices, default=InviteStatus.PENDING)
    sent_at = models.DateTimeField(default=timezone.now)
    accepted_at = models.DateTimeField(blank=True, null=True)
    payload = models.JSONField(default=dict, blank=True)  # optional: license_number, vehicle_type, phone, full_name

