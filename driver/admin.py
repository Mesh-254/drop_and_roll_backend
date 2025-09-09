from django.contrib import admin
from django.contrib import admin
from unfold.admin import ModelAdmin
from driver.models import DriverAvailability, DriverPayout, DriverRating, DriverProfile, DriverDocument, \
    DriverInvitation


@admin.register(DriverAvailability)
class DriverAvailabilityAdmin(ModelAdmin):
    list_display = ("get_driver_name", "available", "lat", "lng", "last_updated")
    list_filter = ("available",)
    search_fields = ("driver_profile__user__full_name", "driver_profile__user__email")

    def get_driver_name(self, obj):
        return obj.driver_profile.user.full_name if obj.driver_profile and obj.driver_profile.user else "N/A"

    get_driver_name.short_description = "Driver"


@admin.register(DriverPayout)
class DriverPayoutAdmin(ModelAdmin):
    list_display = ("driver_profile", "amount", "status", "payout_date", "created_at")
    list_filter = ("status",)


@admin.register(DriverRating)
class DriverRatingAdmin(admin.ModelAdmin):
    list_display = ("driver_profile", "customer", "booking", "rating", "created_at")
    list_filter = ("rating",)

@admin.register(DriverProfile)
class DriverProfileAdmin(ModelAdmin):
    list_display = ("user", "vehicle_type", "status", "is_verified", "total_deliveries", "rating")
    list_filter = ("vehicle_type", "status", "is_verified")

@admin.register(DriverDocument)
class DriverDocumentAdmin(ModelAdmin):
    list_display = ("driver", "doc_type", "verified", "uploaded_at")
    list_filter = ("doc_type", "verified")


@admin.register(DriverInvitation)
class DriverInvitationAdmin(ModelAdmin):
    list_display = ("email", "full_name", "token", "status","expires_at", "accepted_at")
    readonly_fields = ("token",)