from django.db.models.signals import post_save
from django.dispatch import receiver
from bookings.models import Booking
from .models import DriverShift

@receiver(post_save, sender=Booking)
def update_shift_status_on_booking_change(sender, instance, **kwargs):
    if instance.route and instance.route.shift:
        instance.route.shift.update_status()  # Recompute based on all bookings
