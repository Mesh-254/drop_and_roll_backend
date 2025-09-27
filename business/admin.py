# business/admin.py
from django.contrib import admin
from django.utils import timezone
from unfold.admin import ModelAdmin
import shortuuid
import uuid
from django.contrib import messages
from django.db.transaction import atomic
from .models import BusinessPricing, BusinessInquiry, BusinessInquiryStatus
from .utils.pricing import compute_business_quote
from bookings.models import Quote, Booking, BookingStatus, Address
from payments.models import PaymentTransaction, PaymentStatus
from driver.models import DriverProfile

@admin.register(BusinessPricing)
class BusinessPricingAdmin(ModelAdmin):
    list_display = ('shipping_type', 'service_type', 'base_price_per_kg', 'base_price_per_km', 'fragile_surcharge', 'insurance_rate', 'created_at', 'updated_at')
    list_filter = ('shipping_type', 'service_type', 'created_at')
    search_fields = ('shipping_type__name', 'service_type__name')
    ordering = ('-created_at',)
    fieldsets = (
        (None, {
            'fields': ('shipping_type', 'service_type', 'base_price_per_kg', 'base_price_per_km', 'fragile_surcharge', 'insurance_rate')
        }),
    )

    def get_readonly_fields(self, request, obj=None):
        return ['created_at', 'updated_at']

@admin.register(BusinessInquiry)
class BusinessInquiryAdmin(ModelAdmin):
    list_display = ('business_name', 'contact_person', 'email', 'status', 'created_at', 'pickup_address_display', 'dropoff_address_display')
    list_filter = ('status', 'created_at')
    search_fields = ('business_name', 'contact_person', 'email', 'admin_notes')
    ordering = ('-created_at',)
    fieldsets = (
        (None, {
            'fields': ('business_name', 'contact_person', 'email', 'phone', 'description')
        }),
        ('Addresses', {
            'fields': ('pickup_address', 'dropoff_address')
        }),
        ('Status & Notes', {
            'fields': ('status', 'admin_notes')
        }),
    )
    readonly_fields = ('created_at', 'updated_at')

    def pickup_address_display(self, obj):
        return str(obj.pickup_address) if obj.pickup_address else "None"
    pickup_address_display.short_description = "Pickup Address"

    def dropoff_address_display(self, obj):
        return str(obj.dropoff_address) if obj.dropoff_address else "None"
    dropoff_address_display.short_description = "Dropoff Address"

    def get_readonly_fields(self, request, obj=None):
        return ['created_at', 'updated_at']