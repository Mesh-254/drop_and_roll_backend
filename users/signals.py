import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db.models.signals import post_save
from django.db.models.signals import pre_save
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone

from driver.models import DriverInvitation
from driver.models import DriverProfile
from .models import CustomerProfile, AdminProfile
from .tasks import send_confirmation_email, send_reset_email, send_welcome_email
from django.dispatch import Signal


User = get_user_model()
logger = logging.getLogger(__name__)


@receiver(post_save, sender=User)
def create_role_profile(sender, instance: User, created, **kwargs):
    if not created:
        return
    if instance.role == User.Role.CUSTOMER and not hasattr(
        instance, "customer_profile"
    ):
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
    if instance.role == User.Role.CUSTOMER and not hasattr(
        instance, "customer_profile"
    ):
        CustomerProfile.objects.get_or_create(user=instance)
    elif instance.role == User.Role.DRIVER and not hasattr(instance, "driver_profile"):
        DriverProfile.objects.get_or_create(user=instance)
    elif instance.role == User.Role.ADMIN and not hasattr(instance, "admin_profile"):
        AdminProfile.objects.get_or_create(user=instance)


@receiver(post_save, sender=User)
def send_driver_invitation_on_create(sender, instance, created, **kwargs):
    if created and instance.role == User.Role.DRIVER:
        logger.debug(f"New driver created: {instance.email}")
        # Check if an invitation already exists
        existing_invitation = DriverInvitation.objects.filter(
            email=instance.email, status=DriverInvitation.Status.PENDING
        ).first()
        if existing_invitation and not existing_invitation.is_expired():
            logger.warning(f"Active invitation already exists for {instance.email}")
            return

        # Create invitation
        invitation = DriverInvitation.objects.create(
            email=instance.email,
            full_name=instance.full_name,
            created_by=None,
            expires_at=timezone.now() + timedelta(days=7),
            status=DriverInvitation.Status.PENDING,
        )

        try:
            # Generate invitation URL
            invitation_url = f"{settings.SITE_URL}{reverse('driver:accept_invitation', kwargs={'token': str(invitation.token)})}"
            logger.debug(f"Generated invitation URL: {invitation_url}")

            # Send email
            subject = "Driver Invitation - Set Up Your Account"
            message = (
                f"Dear {instance.full_name},\n\n"
                f"You have been invited to join as a driver. Please use the following link to set up your account:\n\n"
                f"{invitation_url}\n\n"
                f"This link expires on {invitation.expires_at.strftime('%Y-%m-%d %H:%M')}.\n\n"
                f"Best regards,\nThe Admin Team"
            )
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [instance.email],
                fail_silently=False,
            )
            logger.info(f"Invitation sent to {instance.email}")
        except Exception as e:
            logger.error(f"Failed to send invitation to {instance.email}: {str(e)}")


@receiver(pre_save, sender=User)
def capture_old_user_state(sender, instance, **kwargs):
    """
    Capture old state before save to detect changes in post_save.
    Mimics payments' capture_old_status for clean delta detection.
    """
    if instance.pk:
        try:
            old_instance = sender.objects.get(pk=instance.pk)
            instance._old_role = old_instance.role
            instance._old_is_active = old_instance.is_active
        except sender.DoesNotExist:
            instance._old_role = None
            instance._old_is_active = None
    else:
        instance._old_role = None
        instance._old_is_active = None


@receiver(post_save, sender=User)
def send_welcome_on_activation(sender, instance: User, created, **kwargs):
    """
    Send welcome email on activation change (to True).
    Only for customers; fires on save if delta detected.
    """
    if created or instance.role != User.Role.CUSTOMER:
        # No email on create (use confirmation flow); skip non-customers
        return

    old_active = getattr(instance, "_old_is_active", False)
    if instance.is_active and not old_active:
        # Activated (e.g., via confirmation or admin toggle)
        try:
            subject = "Welcome to Drop 'N Roll!"
            context = {
                "full_name": instance.full_name,
                "email": instance.email,
                "site_name": "Drop 'n Roll",
                "support_email": settings.DEFAULT_FROM_EMAIL,
                "site_url": getattr(settings, "FRONTEND_URL"),
            }
            send_welcome_email.delay(
                subject=subject,
                context=context,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[instance.email],
            )
            logger.info(
                f"Welcome email queued for activated customer: {instance.email}"
            )
        except Exception as e:
            logger.error(
                f"Failed to queue welcome email for {instance.email}: {str(e)}"
            )
