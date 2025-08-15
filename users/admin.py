from django.contrib import admin
from django.contrib.auth import get_user_model

from .models import CustomerProfile, DriverProfile, AdminProfile, DriverDocument, DriverInvitation

User = get_user_model()


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("email", "full_name", "role", "is_active", "date_joined")
    list_filter = ("role", "is_active", "date_joined")
    search_fields = ("email", "full_name", "phone")
    readonly_fields = ("date_joined",)


@admin.register(DriverProfile)
class DriverProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "vehicle_type", "status", "is_verified", "total_deliveries", "rating")
    list_filter = ("vehicle_type", "status", "is_verified")


@admin.register(CustomerProfile)
class CustomerProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "default_pickup_address", "default_dropoff_address", "preferred_payment_method")


@admin.register(AdminProfile)
class AdminProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "department", "access_level")


@admin.register(DriverDocument)
class DriverDocumentAdmin(admin.ModelAdmin):
    list_display = ("driver", "doc_type", "verified", "uploaded_at")
    list_filter = ("doc_type", "verified")


@admin.register(DriverInvitation)
class DriverInvitationAdmin(admin.ModelAdmin):
    list_display = ("email", "full_name", "token", "expires_at", "accepted_at")
    readonly_fields = ("token",)
