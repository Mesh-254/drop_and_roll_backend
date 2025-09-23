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
    list_display = ('business_name', 'contact_person', 'email', 'status', 'created_at', 'quote', 'booking')
    list_filter = ('status', 'created_at', 'shipping_type', 'service_type')
    search_fields = ('business_name', 'contact_person', 'email', 'admin_notes')
    ordering = ('-created_at',)
    fieldsets = (
        (None, {
            'fields': ('business_name', 'contact_person', 'email', 'phone', 'description', 'user')
        }),
        ('Quote Details', {
            'fields': ('shipping_type', 'service_type', 'weight_kg', 'distance_km', 'fragile', 'insurance_amount', 'dimensions', 'pickup_address', 'dropoff_address')
        }),
        ('Status & Links', {
            'fields': ('status', 'quote', 'booking', 'admin_notes')
        }),
    )
    readonly_fields = ('created_at', 'updated_at', 'quote', 'booking')
    actions = ['generate_quote', 'create_booking', 'approve_booking', 'assign_driver']

    def get_readonly_fields(self, request, obj=None):
        fields = ['created_at', 'updated_at']
        if obj and obj.quote:
            fields.append('quote')
        if obj and obj.booking:
            fields.append('booking')
        return fields

    @admin.action(description="Generate Quote for Selected Inquiries")
    @atomic
    def generate_quote(self, request, queryset):
        for inquiry in queryset:
            if inquiry.status != BusinessInquiryStatus.PENDING:
                self.message_user(request, f"Cannot generate quote for {inquiry.business_name}: not in PENDING status", level=messages.WARNING)
                continue
            try:
                base_price, final_price, breakdown = compute_business_quote(
                    shipping_type=inquiry.shipping_type,
                    service_type=inquiry.service_type,
                    weight_kg=inquiry.weight_kg or 0,
                    distance_km=inquiry.distance_km or 0,
                    fragile=inquiry.fragile or False,
                    insurance_amount=inquiry.insurance_amount or 0,
                    dimensions=inquiry.dimensions or {},
                    surge=1,
                    discount=0,
                )
                quote = Quote.objects.create(
                    shipping_type=inquiry.shipping_type,
                    service_type=inquiry.service_type,
                    weight_kg=inquiry.weight_kg,
                    distance_km=inquiry.distance_km,
                    fragile=inquiry.fragile,
                    insurance_amount=inquiry.insurance_amount,
                    dimensions=inquiry.dimensions,
                    base_price=base_price,
                    surge_multiplier=1,
                    discount_amount=0,
                    final_price=final_price,
                    meta=breakdown,
                )
                inquiry.quote = quote
                inquiry.status = BusinessInquiryStatus.QUOTED
                inquiry.save()
                self.message_user(request, f"Quote generated for {inquiry.business_name}", level=messages.SUCCESS)
            except Exception as e:
                self.message_user(request, f"Failed to generate quote for {inquiry.business_name}: {str(e)}", level=messages.ERROR)

    @admin.action(description="Create Booking for Selected Inquiries")
    @atomic
    def create_booking(self, request, queryset):
        for inquiry in queryset:
            if inquiry.status != BusinessInquiryStatus.QUOTED or not inquiry.quote:
                self.message_user(request, f"Cannot create booking for {inquiry.business_name}: not in QUOTED status or no quote", level=messages.WARNING)
                continue
            try:
                booking = Booking.objects.create(
                    customer=inquiry.user if inquiry.user and inquiry.user.role == 'customer' else None,
                    guest_email=inquiry.email if not inquiry.user else None,
                    quote=inquiry.quote,
                    final_price=inquiry.quote.final_price,
                    pickup_address=inquiry.pickup_address,
                    dropoff_address=inquiry.dropoff_address,
                    status=BookingStatus.PENDING,
                    notes=inquiry.description,
                    payment_expires_at=timezone.now() + timezone.timedelta(days=1),
                )
                inquiry.booking = booking
                inquiry.status = BusinessInquiryStatus.BOOKED
                inquiry.save()
                if booking.final_price > 0:
                    PaymentTransaction.objects.create(
                        user=booking.customer,
                        guest_email=booking.guest_email,
                        booking=booking,
                        amount=booking.final_price,
                        status=PaymentStatus.PENDING,
                        reference=str(uuid.uuid4())[:12].replace("-", "")
                    )
                self.message_user(request, f"Booking created for {inquiry.business_name}", level=messages.SUCCESS)
            except Exception as e:
                self.message_user(request, f"Failed to create booking for {inquiry.business_name}: {str(e)}", level=messages.ERROR)

    @admin.action(description="Approve Selected Bookings")
    def approve_booking(self, request, queryset):
        for inquiry in queryset:
            if inquiry.status != BusinessInquiryStatus.BOOKED or not inquiry.booking:
                self.message_user(request, f"Cannot approve booking for {inquiry.business_name}: not in BOOKED status or no booking", level=messages.WARNING)
                continue
            try:
                booking = inquiry.booking
                booking.status = BookingStatus.SCHEDULED
                booking.tracking_number = f"BK-{shortuuid.uuid()[:6].upper()}"
                booking.save()
                self.message_user(request, f"Booking approved for {inquiry.business_name}", level=messages.SUCCESS)
            except Exception as e:
                self.message_user(request, f"Failed to approve booking for {inquiry.business_name}: {str(e)}", level=messages.ERROR)

    @admin.action(description="Assign Driver to Selected Bookings")
    def assign_driver(self, request, queryset):
        driver_id = request.POST.get('driver_profile_id')
        if not driver_id:
            self.message_user(request, "Driver profile ID required. Please provide it in the action form.", level=messages.ERROR)
            return
        try:
            DriverProfile.objects.get(id=driver_id)
        except DriverProfile.DoesNotExist:
            self.message_user(request, f"Invalid driver profile ID: {driver_id}", level=messages.ERROR)
            return
        for inquiry in queryset:
            if inquiry.status != BusinessInquiryStatus.BOOKED or not inquiry.booking:
                self.message_user(request, f"Cannot assign driver for {inquiry.business_name}: not in BOOKED status or no booking", level=messages.WARNING)
                continue
            try:
                booking = inquiry.booking
                booking.driver_id = driver_id
                booking.status = BookingStatus.ASSIGNED
                booking.save()
                self.message_user(request, f"Driver assigned to booking for {inquiry.business_name}", level=messages.SUCCESS)
            except Exception as e:
                self.message_user(request, f"Failed to assign driver for {inquiry.business_name}: {str(e)}", level=messages.ERROR)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        # Optionally customize form fields, e.g., make driver_id selectable for assign_driver
        return form