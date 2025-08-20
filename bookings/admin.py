from django.contrib import admin
from django.utils.html import format_html
from unfold.admin import ModelAdmin

from bookings.models import Address, Quote, Booking, ShippingType,ServiceType


@admin.register(Address)
class AddressAdmin(ModelAdmin):
    list_display = ("line1", "city", "region", "country", "validated")
    search_fields = ("line1", "city", "region", "postal_code")


@admin.register(Quote)
class QuoteAdmin(ModelAdmin):
    list_display = ("created_at", "service_tier", "weight_kg", "distance_km", "final_price")
    list_filter = ("service_tier",)
    readonly_fields = ("created_at",)


@admin.register(Booking)
class BookingAdmin(ModelAdmin):
    list_display = ("id", "customer", "service_tier", "status_badge", "final_price", "created_at")
    list_filter = ("status", "service_tier")
    search_fields = ("id", "customer__email", "customer__full_name")
    readonly_fields = ("created_at", "updated_at")

    def status_badge(self, obj):
        color = {
            "pending": "#999",
            "scheduled": "#0ea5e9",
            "assigned": "#f59e0b",
            "picked_up": "#6366f1",
            "in_transit": "#06b6d4",
            "delivered": "#16a34a",
            "cancelled": "#ef4444",
            "failed": "#b91c1c",
        }.get(obj.status, "#444")
        return format_html('<span style="padding:4px 8px;border-radius:9999px;color:#fff;background:{}">{}</span>',
                           color, obj.get_status_display())

    status_badge.short_description = "Status"


@admin.register(ShippingType)
class ShippingTypeAdmin(ModelAdmin):
    list_display = ("id", "name", "description", "created_at", "updated_at")
    search_fields = ("name",)
    list_filter = ("created_at", "updated_at")


@admin.register(ServiceType)
class ServiceTypeAdmin(ModelAdmin):
    list_display = ("id", "name", "type", "price", "created_at", "updated_at")
    search_fields = ("name", "type")
    list_filter = ("type", "created_at", "updated_at")
