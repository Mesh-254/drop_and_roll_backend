from django.contrib import admin
from driver.models import (
    DriverProfile, DriverDocument, DriverAvailability, DriverPayout, DriverRating, DriverInvite
)

@admin.register(DriverProfile)
class DriverProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "vehicle_type", "license_number", "is_verified", "status", "rating_avg", "rating_count")
    search_fields = ("user__email", "license_number")
    list_filter = ("status", "is_verified")

@admin.register(DriverDocument)
class DriverDocumentAdmin(admin.ModelAdmin):
    list_display = ("id", "driver_profile", "document_type", "status", "uploaded_at", "reviewed_at")
    list_filter = ("document_type", "status")
    search_fields = ("driver_profile__user__email",)

@admin.register(DriverAvailability)
class DriverAvailabilityAdmin(admin.ModelAdmin):
    list_display = ("driver_profile", "available", "lat", "lng", "last_updated")
    list_filter = ("available",)

@admin.register(DriverPayout)
class DriverPayoutAdmin(admin.ModelAdmin):
    list_display = ("driver_profile", "amount", "status", "payout_date", "created_at")
    list_filter = ("status",)

@admin.register(DriverRating)
class DriverRatingAdmin(admin.ModelAdmin):
    list_display = ("driver_profile", "customer", "rating", "created_at")
    list_filter = ("rating",)

@admin.register(DriverInvite)
class DriverInviteAdmin(admin.ModelAdmin):
    list_display = ("email", "invited_by", "status", "token", "sent_at", "accepted_at")
    list_filter = ("status",)
    search_fields = ("email",)
