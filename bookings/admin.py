from django.contrib import admin
from django.utils.html import format_html
from unfold.admin import ModelAdmin
from .models import Address, Quote, Booking, ShippingType, ServiceType, RecurringSchedule, BulkUpload


@admin.register(Address)
class AddressAdmin(ModelAdmin):
    list_display = ("line1", "city", "region", "country", "validated")
    search_fields = ("line1", "city", "region", "postal_code")
    list_filter = ("city", "country", "validated")


@admin.register(Quote)
class QuoteAdmin(ModelAdmin):
    list_display = ("id", "get_service_type_name", "get_shipping_type_name",
                    "weight_kg", "distance_km", "final_price", "created_at")
    list_filter = ("service_type__name", "shipping_type__name", "created_at")
    search_fields = ("service_type__name", "shipping_type__name")
    readonly_fields = ("created_at",)

    def get_service_type_name(self, obj):
        return obj.service_type.name if obj.service_type else "N/A"
    get_service_type_name.short_description = "Service Type"

    def get_shipping_type_name(self, obj):
        return obj.shipping_type.name if obj.shipping_type else "N/A"
    get_shipping_type_name.short_description = "Shipping Type"


@admin.register(Booking)
class BookingAdmin(ModelAdmin):
    list_display = ("id", "get_customer_name", "get_service_type_name",
                    "status_badge", "final_price", "created_at")
    list_filter = ("status", "quote__service_type__name", "created_at")
    search_fields = ("id", "customer__email",
                     "customer__full_name", "guest_email")
    readonly_fields = ("created_at", "updated_at")

    def get_customer_name(self, obj):
        return obj.customer.full_name if obj.customer else obj.guest_email or "Anonymous"
    get_customer_name.short_description = "Customer"

    def get_service_type_name(self, obj):
        return obj.quote.service_type.name if obj.quote and obj.quote.service_type else "N/A"
    get_service_type_name.short_description = "Service Type"

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
    search_fields = ("name", "description")
    list_filter = ("created_at", "updated_at")


@admin.register(ServiceType)
class ServiceTypeAdmin(ModelAdmin):
    list_display = ("id", "name", "price", "created_at", "updated_at")
    search_fields = ("name", "description")
    list_filter = ("name", "created_at", "updated_at")


@admin.register(RecurringSchedule)
class RecurringScheduleAdmin(ModelAdmin):
    list_display = ("id", "customer", "recurrence",
                    "next_run_at", "active", "created_at")
    list_filter = ("recurrence", "active", "created_at")
    search_fields = ("customer__email", "customer__full_name")


@admin.register(BulkUpload)
class BulkUploadAdmin(ModelAdmin):
    list_display = ("id", "customer", "created_at", "processed")
    list_filter = ("processed", "created_at")
    search_fields = ("customer__email", "customer__full_name")
