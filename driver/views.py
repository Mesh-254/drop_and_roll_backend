import logging
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views import View
from django.http import JsonResponse
from .forms import DriverInvitationForm
from .models import DriverInvitation, DriverProfile

User = get_user_model()
logger = logging.getLogger(__name__)

class AcceptDriverInvitationView(View):
    template_name = "driver/accept_invitation.html"

    def get(self, request, token):
        logger.debug(f"GET request for invitation token: {token}")
        # Clear any stale messages
        storage = messages.get_messages(request)
        storage.used = True

        try:
            invitation = DriverInvitation.objects.get(token=token, status=DriverInvitation.Status.PENDING)
            if invitation.is_expired():
                invitation.status = DriverInvitation.Status.EXPIRED
                invitation.save()
                messages.error(request, "This invitation has expired.")
                logger.warning(f"Expired invitation accessed: {token}")
                return redirect(f"{settings.FRONTEND_URL}/login")
        except DriverInvitation.DoesNotExist:
            messages.error(request, "Invalid or already accepted invitation.")
            logger.warning(f"Invalid invitation token: {token}")
            return redirect(f"{settings.FRONTEND_URL}/login")

        form = DriverInvitationForm()
        return render(request, self.template_name, {
            "form": form,
            "invitation": invitation,
            "frontend_url": settings.FRONTEND_URL
        })

    def post(self, request, token):
        logger.debug(f"POST request for invitation token: {token}")
        try:
            # invitation = DriverInvitation.objects.get(token=token, status=DriverInvitation.Status.PENDING)
            invitation = DriverInvitation.objects.get(token=token)
            if invitation.is_expired():
                invitation.status = DriverInvitation.Status.EXPIRED
                invitation.save()
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({"status": "error", "message": "This invitation has expired."})
                messages.error(request, "This invitation has expired.")
                logger.warning(f"Expired invitation submitted: {token}")
                return redirect(f"{settings.FRONTEND_URL}/login")
        except DriverInvitation.DoesNotExist:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({"status": "error", "message": "Invalid or already accepted invitation."})
            messages.error(request, "Invalid or already accepted invitation.")
            logger.warning(f"Invalid invitation token: {token}")
            return redirect(f"{settings.FRONTEND_URL}/login")

        form = DriverInvitationForm(request.POST)
        if form.is_valid():
            try:
                # Get user
                user = User.objects.get(email=invitation.email)
                user.set_password(form.cleaned_data["password"])
                user.is_active = True
                user.save()
                logger.info(f"User {user.email} password updated and activated")

                # Check for existing DriverProfile and update or create
                driver_profile, created = DriverProfile.objects.get_or_create(
                    user=user,
                    defaults={
                        "license_number": form.cleaned_data["license_number"],
                        "vehicle_type": form.cleaned_data["vehicle_type"],
                        "vehicle_registration": form.cleaned_data["vehicle_registration"] or None,
                        "status": DriverProfile.Status.ACTIVE,
                    }
                )
                if not created:
                    # Update existing profile
                    driver_profile.license_number = form.cleaned_data["license_number"]
                    driver_profile.vehicle_type = form.cleaned_data["vehicle_type"]
                    driver_profile.vehicle_registration = form.cleaned_data["vehicle_registration"] or None
                    driver_profile.status = DriverProfile.Status.ACTIVE
                    driver_profile.save()
                logger.info(f"DriverProfile {'created' if created else 'updated'} for {user.email} with status ACTIVE")

                # Mark invitation as accepted
                invitation.accepted_at = timezone.now()
                invitation.status = DriverInvitation.Status.ACCEPTED
                invitation.save()
                logger.info(f"Invitation accepted: {token}")

                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({"status": "success", "message": "Your account has been set up successfully. Please log in."})
                messages.success(request, "Your account has been set up successfully. Please log in.")
                return redirect(f"{settings.FRONTEND_URL}/login")
            except Exception as e:
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({"status": "error", "message": f"Error setting up account: {str(e)}"})
                messages.error(request, f"Error setting up account: {str(e)}")
                logger.error(f"Error processing invitation for {invitation.email}: {str(e)}")
        else:
            logger.warning(f"Form validation failed for invitation: {token}")
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({"status": "error", "message": "Form validation failed.", "errors": form.errors.as_json()})

        return render(request, self.template_name, {
            "form": form,
            "invitation": invitation,
            "frontend_url": settings.FRONTEND_URL
        })