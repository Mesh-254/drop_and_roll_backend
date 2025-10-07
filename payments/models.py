import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.core.validators import ValidationError


class PaymentMethodType(models.TextChoices):
    MPESA = "mpesa", "M-Pesa"
    CARD = "card", "Card"
    BANK = "bank", "Bank Transfer"
    WALLET = "wallet", "Wallet"
    PAYPAL = "paypal", "PayPal/Google Pay"
    STRIPE = 'stripe', 'Stripe'


class PaymentStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SUCCESS = "success", "Success"
    FAILED = "failed", "Failed"
    REFUNDED = "refunded", "Refunded"


class PaymentMethod(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="payment_methods", null=True, blank=True)
    method_type = models.CharField(
        max_length=16, choices=PaymentMethodType.choices)
    # e.g., Safaricom, Visa, Mastercard
    provider = models.CharField(max_length=64, blank=True, default="")
    # e.g., phone number or card number
    account_ref = models.CharField(max_length=128, blank=True, default="")
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.user.email} - {self.method_type}"


class PaymentTransaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL,
                             on_delete=models.CASCADE, related_name="payments", null=True, blank=True)
    guest_email = models.EmailField(null=True, blank=True)
    method = models.ForeignKey(PaymentMethod, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="transactions")
    booking = models.ForeignKey(
        "bookings.Booking", on_delete=models.SET_NULL, null=True, blank=True, related_name='payment_transactions')
    amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(
        max_length=3,  # ISO 4217 (e.g., 'USD', 'EUR')
        choices=[
            ('USD', 'US Dollar'),
            ('EUR', 'Euro'),
            ('GBP', 'British Pound'),
            # Add more as needed, or use a dynamic choices loader
        ],
        default='GBP',  # Or your primary currency
        null=True, blank=False
    )
    status = models.CharField(
        max_length=16, choices=PaymentStatus.choices, default=PaymentStatus.PENDING)
    reference = models.CharField(max_length=64, unique=True)  # transaction ref
    gateway_response = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # NEW: Query optimization
        indexes = [models.Index(fields=['status', 'booking'])]

    def clean(self):
        if not self.user and not self.guest_email:
            raise ValidationError(
                "Either user or guest_email must be provided.")

    def save(self, *args, **kwargs):
        if self.guest_email:
            self.guest_email = self.guest_email.lower()
        super().save(*args, **kwargs)

    def get_total_refunded(self):
        """For full-only: Return refund amount if processed, else 0."""
        try:
            refund = self.refunds
            return refund.amount if refund.status == 'processed' else Decimal('0.00')
        except Refund.DoesNotExist:
            return Decimal('0.00')

    def is_fully_refunded(self):
        """True if processed refund exists."""
        try:
            refund = self.refunds
            return refund.status == 'processed'
        except Refund.DoesNotExist:
            return False

    def __str__(self):
        # Fallback for old data during transition
        currency_display = self.currency or 'USD'
        return f"Tx {self.reference} ({self.amount} {currency_display}) - {self.status}"


class Wallet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wallet")
    balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"))
    updated_at = models.DateTimeField(auto_now=True)


class Refund(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processed', 'Processed'),
        ('failed', 'Failed'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transaction = models.OneToOneField(PaymentTransaction, on_delete=models.CASCADE,
                                       related_name='refunds')  # NEW: related_name for tx.refunds
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default='pending')  # NEW: Track processing
    admin_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)  # NEW: Auditor
    refunded_at = models.DateTimeField(default=timezone.now)
    gateway_response = models.JSONField(default=dict, blank=True)

    class Meta:
        # Perf for queries
        indexes = [models.Index(fields=['status', 'refunded_at'])]

    def __str__(self):
        return f"Refund {self.id} ({self.amount}) - {self.status}"
