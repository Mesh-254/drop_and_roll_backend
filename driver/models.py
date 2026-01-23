from __future__ import annotations

from datetime import timedelta, datetime, time
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone
from bookings.models import Route


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
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="driver_profile"
    )
    license_number = models.CharField(max_length=50)
    vehicle_type = models.CharField(max_length=20, choices=Vehicle.choices)
    vehicle_registration = models.CharField(max_length=50, blank=True, null=True)
    is_verified = models.BooleanField(default=False)
    is_tracking_enabled = models.BooleanField(default=False)  # Manual toggle
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.INACTIVE
    )

    # Performance
    total_deliveries = models.PositiveIntegerField(default=0)
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=0.00)

    hub = models.ForeignKey(
        "bookings.Hub",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="drivers",
    )

    max_weight_kg = models.DecimalField(
        max_digits=10, decimal_places=2, default=100.0
    )  # e.g., bike=10, truck=1000
    max_volume_m3 = models.DecimalField(
        max_digits=10, decimal_places=2, default=1.0
    )  # Add for dimensions

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Auto-create today's shift if missing
        DriverShift.get_or_create_today(self)

    def recompute_availability(self):
        if not hasattr(self, "availability"):
            self.availability = DriverAvailability.objects.create(
                driver_profile=self, available=False
            )

        has_active = (
            self.assigned_routes.filter(status__in=["assigned", "in_progress"]).exists()
            or self.assigned_bookings.filter(
                status__in=["assigned", "in_progress"]
            ).exists()
        )
        availability = (
            self.availability
            if hasattr(self, "availability")
            else DriverAvailability.objects.get_or_create(driver_profile=self)[0]
        )
        availability.available = (
            not has_active
        )  # True if no active routes/bookings, else False
        availability.save(update_fields=["available"])
        return availability.available
    
    def __str__(self):
        hub_str = f" [{self.hub.name}]" if self.hub else " [No Hub]"
        return f"{self.user.get_full_name()}:{self.user.email} - {self.user.role} {hub_str}"


class DriverShift(models.Model):
    """
    Represents a single 8-hour shift for a driver.
    - start_time: When shift begins (e.g., 08:00)
    - end_time: When shift ends (start_time + 8h)
    - current_load: JSON dict tracking used capacity:
        {
            "weight": 45.5,     # kg used
            "volume": 0.8,     # m³ used
            "hours": 3.2       # hours of work assigned
        }
    - remaining_hours: Computed property
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"  # Unassigned, waiting for driver
        ASSIGNED = "assigned", "Assigned"  # Driver assigned, but not started
        ACTIVE = "active", "Active"  # Driver has started (e.g., first pickup)
        COMPLETED = "completed", "Completed"  # All bookings done
        OVERDUE = "overdue", "Overdue"  # Past end_time, not completed
        CANCELLED = "cancelled", "Cancelled"  # Manual cancel

    driver = models.ForeignKey(
        DriverProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="shifts",
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()  # Must be start_time + 8 hours
    max_hours = models.FloatField(default=8.0)
    current_load = models.JSONField(default=dict)

    MIN_ROUTE_HOURS = 4.0  # Configurable threshold for "short" routes

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )

    class Meta:
        indexes = [
            models.Index(fields=["driver", "status"]),  # For fast checks on open shifts
            models.Index(fields=["start_time", "end_time"]),  # For overdue queries
        ]

    def save(self, *args, **kwargs):
        # NEW: Initialize current_load with zeros if not set
        if not self.current_load:
            self.current_load = {"weight": 0.0, "volume": 0.0, "hours": 0.0}

        # Auto-compute overdue on save (for manual updates)
        if self.end_time < timezone.now() and self.status not in [
            self.Status.COMPLETED,
            self.Status.OVERDUE,
            self.Status.CANCELLED,
        ]:
            self.status = self.Status.OVERDUE
        super().save(*args, **kwargs)

    @property
    def is_open(self) -> bool:
        """Check if shift is ongoing (for assignment rules)."""
        return self.status in [self.Status.ASSIGNED, self.Status.ACTIVE]

    def update_status(self):
        if self.status == self.Status.OVERDUE:
            return  # Don't change overdue shifts automatically
        if not self.routes.exists():
            self.status = self.Status.PENDING
        elif self.routes.filter(status__in=["assigned", "in_progress"]).exists():
            self.status = self.Status.ACTIVE
        else:
            self.status = self.Status.COMPLETED
        self.save(update_fields=["status"])

    @property
    def remaining_hours(self) -> float:
        """How many hours are still free in this shift."""
        used = self.current_load.get("hours", 0.0)
        return max(0.0, self.max_hours - used)

    def __str__(self):
        return f"{self.driver} {self.start_time.date()} ({self.start_time.strftime('%H:%M')} - {self.end_time.strftime('%H:%M')})"

    @classmethod
    def get_or_create_today(cls, driver_profile):
        """
        Get or create the DriverShift for the current day.

        Important characteristics:
        - Uses current time to determine "today"
        - Creates shift with 06:00–18:00 window based on when it's first called
        - Does NOT require the 'date' field (can be removed from model if desired)
        - Safe for concurrent calls (get_or_create is atomic)
        - Sets status to PENDING only if no routes exist yet

        Returns:
            DriverShift: The shift object for today (always returns object, never tuple)
        """
        now = timezone.now()
        today_start = now.replace(hour=6, minute=0, second=0, microsecond=0)
        today_end = now.replace(hour=18, minute=0, second=0, microsecond=0)

        shift, created = cls.objects.get_or_create(
            driver=driver_profile,
            # date=today,           # ← commented out → we don't need it anymore
            defaults={
                "current_load": {"hours": 0.0, "weight": 0.0, "volume": 0.0},
                "start_time": today_start,
                "end_time": today_end,
                # We don't set status here — we handle it below
            },
        )

        # If this is a newly created shift OR it has no routes yet → make sure it's PENDING
        if created or not shift.routes.exists():
            if shift.status != cls.Status.PENDING:
                shift.status = cls.Status.PENDING
                shift.save(update_fields=["status"])
        # If it already has routes, we preserve whatever status it currently has
        # (usually ASSIGNED/ACTIVE/COMPLETED)

        return shift


class DriverDocument(models.Model):
    """KYC documents for drivers (license scan, insurance, national ID)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    driver = models.ForeignKey(
        DriverProfile, on_delete=models.CASCADE, related_name="documents"
    )
    doc_type = models.CharField(max_length=50)  # e.g., license, insurance, id
    file = models.FileField(upload_to="drivers/docs/")
    uploaded_at = models.DateTimeField(default=timezone.now)
    verified = models.BooleanField(default=False)
    notes = models.TextField(blank=True, null=True)

    class Meta:
        # Ensure one doc_type per driver
        unique_together = ("driver", "doc_type")

    def __str__(self):
        return f"DriverDocument({self.doc_type}, {self.driver.user.email}, {self.driver.user.role})"


class DriverInvitation(models.Model):
    """Admin invites a driver; driver accepts and sets password via token."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        EXPIRED = "expired", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField()
    full_name = models.CharField(max_length=255)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="driver_invites_created",
    )
    token = models.UUIDField(default=uuid.uuid4, unique=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(blank=True, null=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    def save(self, *args, **kwargs):
        if self.is_expired() and self.status != self.Status.ACCEPTED:
            self.status = self.Status.EXPIRED
        super().save(*args, **kwargs)

    def __str__(self):
        return f"DriverInvitation({self.email})"


class DriverAvailability(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    driver_profile = models.OneToOneField(
        DriverProfile, on_delete=models.CASCADE, related_name="availability"
    )
    available = models.BooleanField(default=False)
    lat = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    lng = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    last_updated = models.DateTimeField(default=timezone.now)


class DriverPayout(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    driver_profile = models.ForeignKey(
        DriverProfile, on_delete=models.CASCADE, related_name="payouts"
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(
        max_length=16, choices=PayoutStatus.choices, default=PayoutStatus.PENDING
    )
    payout_date = models.DateTimeField(blank=True, null=True)
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)


class DriverRating(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    driver_profile = models.ForeignKey(
        DriverProfile, on_delete=models.CASCADE, related_name="ratings"
    )
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="driver_ratings",
    )
    # Optional link to booking (if bookings app is installed)
    booking = models.ForeignKey(
        "bookings.Booking", on_delete=models.SET_NULL, null=True, blank=True
    )
    rating = models.PositiveSmallIntegerField()
    comment = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("driver_profile", "customer", "booking")


# New model for driver location tracking
class DriverLocation(models.Model):
    """
    Stores real-time and historical location updates from drivers.
    One row per location update → enables breadcrumbs, playback, analytics.
    """

    driver_profile = models.ForeignKey(
        DriverProfile, on_delete=models.CASCADE, related_name="locations"
    )

    # Core GPS data
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)

    # Optional enriched data from mobile device
    speed_kmh = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True
    )
    heading_degrees = models.IntegerField(null=True, blank=True)  # 0-359
    accuracy_meters = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True
    )
    altitude_meters = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True
    )

    # Source info
    source = models.CharField(
        max_length=20,
        choices=[
            ("mobile_app", "Mobile App"),
            ("web_browser", "Web Browser"),
            ("manual", "Manual"),
        ],
        default="mobile_app",
    )

    # Timestamp
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    # Optional: Tie to active route
    route = models.ForeignKey(
        "bookings.Route", on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        verbose_name = "Driver Location Update"
        verbose_name_plural = "Driver Location Updates"
        ordering = ["-timestamp"]
        indexes = [
            models.Index(
                fields=["driver_profile", "-timestamp"]
            ),  # Fast latest per driver
            models.Index(fields=["timestamp"]),  # Time-based queries
        ]
        # Optional: keep only last N days/hours if storage is concern
        # But usually keep forever for analytics

    def __str__(self):
        return f"{self.driver_profile.user.get_full_name()} @ {self.timestamp} ({self.latitude}, {self.longitude})"
