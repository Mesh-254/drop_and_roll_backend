from django.contrib.auth import get_user_model
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from driver.models import DriverProfile
from .models import CustomerProfile, AdminProfile


User = get_user_model()


@receiver(post_save, sender=User)
def create_role_profile(sender, instance: User, created, **kwargs):
    if not created:
        return
    if instance.role == User.Role.CUSTOMER and not hasattr(instance, "customer_profile"):
        CustomerProfile.objects.create(user=instance)
    elif instance.role == User.Role.DRIVER and not hasattr(instance, "driver_profile"):
        DriverProfile.objects.create(user=instance)
    elif instance.role == User.Role.ADMIN and not hasattr(instance, "admin_profile"):
        AdminProfile.objects.create(user=instance)


@receiver(pre_save, sender=User)
def ensure_profile_on_role_change(sender, instance: User, **kwargs):
    if not instance.pk:
        return
    try:
        prev = User.objects.get(pk=instance.pk)
    except User.DoesNotExist:
        return
    if prev.role == instance.role:
        return
    # Create the profile for the new role if missing
    if instance.role == User.Role.CUSTOMER and not hasattr(instance, "customer_profile"):
        CustomerProfile.objects.get_or_create(user=instance)
    elif instance.role == User.Role.DRIVER and not hasattr(instance, "driver_profile"):
        DriverProfile.objects.get_or_create(user=instance)
    elif instance.role == User.Role.ADMIN and not hasattr(instance, "admin_profile"):
        AdminProfile.objects.get_or_create(user=instance)
