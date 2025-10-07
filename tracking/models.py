from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from bookings.models import Booking


class TrackingStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    EN_ROUTE = "en_route", "En Route"
    NEARBY = "nearby", "Nearby"
    DELIVERED = "delivered", "Delivered"
    FAILED = "failed", "Failed"
    CANCELED = "canceled", "Canceled"


class TrackingSession(models.Model):
    """One tracking session per Booking (from bookings app)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.OneToOneField(
        "bookings.Booking", on_delete=models.CASCADE, related_name="tracking_session"
    )
    status = models.CharField(max_length=20, choices=TrackingStatus.choices, default=TrackingStatus.PENDING)
    started_at = models.DateTimeField(blank=True, null=True)
    ended_at = models.DateTimeField(blank=True, null=True)
    last_event_at = models.DateTimeField(blank=True, null=True)
    eta = models.DateTimeField(blank=True, null=True)

    # Public sharing
    public_token = models.UUIDField(default=uuid.uuid4, unique=True)
    public_enabled = models.BooleanField(default=True)

    # Meta info
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"TrackingSession({self.id}) for Booking({self.booking_id})"


class TrackingEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(TrackingSession, on_delete=models.CASCADE, related_name="events")
    code = models.CharField(max_length=50)  # e.g., pickup_scanned, out_for_delivery, arrived, delivered, failed
    message = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]


class DriverLocation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(TrackingSession, on_delete=models.CASCADE, related_name="locations")
    driver = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    lat = models.DecimalField(max_digits=9, decimal_places=6)
    lng = models.DecimalField(max_digits=9, decimal_places=6)
    speed_kph = models.DecimalField(max_digits=6, decimal_places=2, blank=True, null=True)
    heading = models.PositiveSmallIntegerField(blank=True, null=True)
    accuracy_m = models.DecimalField(max_digits=6, decimal_places=2, blank=True, null=True)
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(fields=["session", "recorded_at"]),
        ]
        ordering = ["-recorded_at"]


class Geofence(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    center_lat = models.DecimalField(max_digits=9, decimal_places=6)
    center_lng = models.DecimalField(max_digits=9, decimal_places=6)
    radius_m = models.PositiveIntegerField()
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)


class ProofOfDelivery(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    photo = models.ImageField(upload_to="tracking/pod/photos/", blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    location = models.JSONField(default=dict, blank=True)
    booking= models.ForeignKey(Booking,on_delete=models.SET_NULL,blank=True,null=True,related_name="proof_of_delivery")
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"Proof for Booking {self.booking.id}"


class WebhookSubscription(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tracking_webhooks")
    url = models.URLField()
    secret = models.CharField(max_length=255)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
