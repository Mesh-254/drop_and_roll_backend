from django.contrib import admin

from driver.models import DriverAvailability, DriverPayout, DriverRating


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
    list_display = ("driver_profile", "customer", "booking", "rating", "created_at")
    list_filter = ("rating",)
