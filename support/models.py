from __future__ import annotations
from decimal import Decimal
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinLengthValidator
import uuid

from bookings.models import Booking  # Link to bookings for context
from users.models import User  # Link to users

class TicketType(models.TextChoices):
    INQUIRY = "inquiry", _("Inquiry")
    COMPLAINT = "complaint", _("Complaint")
    DISPUTE = "dispute", _("Dispute")

class TicketPriority(models.TextChoices):
    LOW = "low", _("Low")
    MEDIUM = "medium", _("Medium")
    HIGH = "high", _("High")
    URGENT = "urgent", _("Urgent")

class TicketStatus(models.TextChoices):
    OPEN = "open", _("Open")  # New ticket
    IN_PROGRESS = "in_progress", _("In Progress")  # Assigned/being handled
    RESOLVED = "resolved", _("Resolved")  # Solution provided
    CLOSED = "closed", _("Closed")  # Final, no further action
    ESCALATED = "escalated", _("Escalated")  # To higher support/admin

class Ticket(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets"
    )
    guest_email = models.EmailField(null=True, blank=True)  # For unauthenticated users, like in bookings
    booking = models.ForeignKey(
        Booking, on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets"
    )  # Link to specific booking for efficient handling (e.g., disputes)
    type = models.CharField(max_length=20, choices=TicketType.choices, default=TicketType.INQUIRY)
    priority = models.CharField(max_length=20, choices=TicketPriority.choices, default=TicketPriority.MEDIUM)
    status = models.CharField(max_length=20, choices=TicketStatus.choices, default=TicketStatus.OPEN)
    subject = models.CharField(max_length=255, validators=[MinLengthValidator(5)])
    description = models.TextField(validators=[MinLengthValidator(20)])
    metadata = models.JSONField(default=dict, blank=True,  null=True)  # e.g., {"related_payment_id": "..."}
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_tickets"
    )  # Auditor (auto-set in views)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_tickets"
    )  # Support staff/admin

    class Meta:
        indexes = [
            models.Index(fields=["status", "priority"]),  # For quick filtering in admin/API
            models.Index(fields=["user", "created_at"]),  # User-specific lists
            models.Index(fields=["booking"]),  # Booking-linked queries
            models.Index(fields=["guest_email"]),  # Guest queries
        ]
        ordering = ["-created_at"]  # Newest first
        constraints = [
            models.CheckConstraint(
                check=models.Q(user__isnull=False) | models.Q(guest_email__isnull=False),
                name="ticket_must_have_user_or_guest_email"
            )
        ]

    def __str__(self):
        return f"Ticket {self.id} ({self.type}): {self.subject} - {self.status}"

    def save(self, *args, **kwargs):
        if self.guest_email:
            self.guest_email = self.guest_email.lower()
        super().save(*args, **kwargs)

class TicketComment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="comments")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="ticket_comments"
    )
    content = models.TextField(validators=[MinLengthValidator(5)])
    is_internal = models.BooleanField(default=False)  # Visible only to support/admins
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=["ticket", "created_at"])]  # Threaded comments
        ordering = ["created_at"]  # Chronological

    def __str__(self):
        return f"Comment on {self.ticket.id} by {self.user or 'Guest'}"

class TicketAttachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="support/attachments/")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=["ticket"])]

    def __str__(self):
        return f"Attachment for {self.ticket.id}"
