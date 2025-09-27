# business/models.py
from django.db import models
import uuid
from django.utils import timezone
from bookings.models import Quote, Booking, BookingStatus, ShippingType, ServiceType, Address
from users.models import User  # Assuming User model from users app


class BusinessInquiryStatus(models.TextChoices):
    PENDING = "pending", "Pending Review"
    QUOTED = "quoted", "Quoted"
    BOOKED = "booked", "Booked"
    REJECTED = "rejected", "Rejected"


class BusinessPricing(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shipping_type = models.ForeignKey(ShippingType, on_delete=models.CASCADE, related_name="business_pricings")
    service_type = models.ForeignKey(ServiceType, on_delete=models.CASCADE, related_name="business_pricings")
    base_price_per_kg = models.DecimalField(max_digits=10, decimal_places=2)  # Admin-defined price per kg
    base_price_per_km = models.DecimalField(max_digits=10, decimal_places=2)  # Admin-defined price per km
    fragile_surcharge = models.DecimalField(max_digits=10, decimal_places=2, default=0)  # Surcharge for fragile items
    insurance_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)  # Percentage of item value
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('shipping_type', 'service_type')  # One pricing rule per shipping/service type combo
        indexes = [
            models.Index(fields=["shipping_type", "service_type"]),
        ]

    def __str__(self):
        return f"Pricing for {self.shipping_type.name} - {self.service_type.name}"


class BusinessInquiry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business_name = models.CharField(max_length=255)
    contact_person = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=20, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    # Optional addresses
    pickup_address = models.ForeignKey(Address, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name="business_pickups")
    dropoff_address = models.ForeignKey(Address, on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name="business_dropoffs")
    status = models.CharField(max_length=20, choices=BusinessInquiryStatus.choices,
                              default=BusinessInquiryStatus.PENDING)
    admin_notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"Inquiry {self.id} from {self.business_name} - {self.status}"