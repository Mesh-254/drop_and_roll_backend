# bookings/signals.py
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from .models import PricingRule
from django.core.cache import cache


@receiver([post_save, post_delete], sender=PricingRule)
def clear_pricing_cache(sender, **kwargs):
    cache.delete("pricing_rules")