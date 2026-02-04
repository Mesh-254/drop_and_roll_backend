from django.contrib import admin
from django.utils.html import format_html
from unfold.admin import ModelAdmin
from .models import (
    Address,
    Quote,
    Booking,
    ShippingType,
    ServiceType,
    RecurringSchedule,
    BulkUpload,
    PricingRule,
)
from django import forms
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import path, reverse
from django.contrib import messages
from driver.models import DriverProfile
from unfold.views import UnfoldModelAdminViewMixin
from django.views.generic import TemplateView
from django.core.paginator import Paginator
from decimal import Decimal
from django.shortcuts import get_object_or_404
from bookings.models import BookingStatus
from payments.models import PaymentStatus, PaymentTransaction
from payments.api_views import RefundSerializer
from payments.models import PaymentStatus, Refund
from .models import Route, Hub
from django.db import transaction  # For atomic transactions
from rest_framework import serializers  # Import serializers for ValidationError
from django.contrib.admin import SimpleListFilter
from django.db import models  # For Q queries


@admin.register(Hub)
class HubAdmin(ModelAdmin):
    list_display = (
        "name",
        "get_active_bookings_count",
        "get_completed_bookings_count",
        "get_routes_count",
        "get_assigned_drivers_count",
    )
    search_fields = ("name",)
    readonly_fields = (
        "get_active_bookings_count",
        "get_completed_bookings_count",
        "get_routes_count",
        "get_assigned_drivers_count",
    )

    def get_active_bookings_count(self, obj):
        return obj.hub_bookings.filter(
            status__in=["scheduled", "assigned", "picked_up", "in_transit"]
        ).count()

    get_active_bookings_count.short_description = "Active Bookings"

    def get_completed_bookings_count(self, obj):
        return obj.hub_bookings.filter(status__in=["delivered", "failed"]).count()

    get_completed_bookings_count.short_description = "Completed"

    def get_routes_count(self, obj):
        return obj.routes.count()

    get_routes_count.short_description = "Routes"

    def get_assigned_drivers_count(self, obj):
        return obj.drivers.filter(
            status="active"
        ).count()  # Fixed: Count stationed active drivers directly (not via routes)


class BookingInline(admin.TabularInline):  # Show bookings in Route admin
    model = Route.bookings.through  # For M2M
    extra = 0
    fields = ("booking",)
    raw_id_fields = ("booking",)  # Efficient for large lists
    readonly_fields = ("booking",)
    verbose_name = "Booking in Route"
    verbose_name_plural = "Bookings in Route"


@admin.register(Route)
class RouteAdmin(ModelAdmin):
    list_display = (
        "id",
        "hub",
        "driver_link",
        "leg_type",
        "status",
        "total_time_hours",
        "total_distance_km",
        "booking_count",
    )  # Added 'hub'
    list_filter = ("leg_type", "status", "driver")
    search_fields = ("driver__user__full_name",)
    inlines = [BookingInline]  # View bookings per route
    actions = ["assign_driver_manually", "re_optimize"]

    def get_queryset(self, request):
        # Efficient: Prefetch bookings/driver
        return super().get_queryset(request).prefetch_related("bookings", "driver")

    def assign_driver(self, request, queryset):
        if "apply" in request.POST:
            driver_id = request.POST.get("driver")
            driver = get_object_or_404(DriverProfile, id=driver_id)
            with transaction.atomic():
                for route in queryset:
                    if route.driver:
                        continue  # Skip if already assigned
                    if route.hub and route.hub != driver.hub:
                        continue  # Skip hub mismatch
                    route.driver = driver
                    route.hub = driver.hub if not route.hub else route.hub
                    route.status = "assigned"
                    route.save()
                    route.bookings.update(
                        driver=driver, hub=route.hub, status="assigned"
                    )
                self.message_user(
                    request, "Drivers assigned successfully.", level=messages.SUCCESS
                )
            return HttpResponseRedirect(".")

    def driver_link(self, obj):
        if obj.driver:
            return format_html(
                '<a href="{}">{}</a>',
                reverse("admin:driver_driverprofile_change", args=(obj.driver.id,)),
                obj.driver.user.full_name,
            )
        return "-"

    driver_link.short_description = "Driver"

    def booking_count(self, obj):
        return obj.bookings.count()

    booking_count.short_description = "Bookings"

    def assign_driver_manually(self, request, queryset):
        # Manual assign action
        if "apply" in request.POST:
            driver_id = request.POST.get("driver")
            try:
                driver = DriverProfile.objects.get(id=driver_id, status="active")
                for route in queryset.filter(status="pending"):
                    if route.hub and route.hub != driver.hub:
                        self.message_user(
                            request,
                            f"Skipped route {route.id}: Hub mismatch with driver hub",
                            level=messages.WARNING,
                        )
                        continue
                    route.driver = driver
                    route.hub = (
                        driver.hub if not route.hub else route.hub
                    )  # Set hub if not set
                    route.status = "assigned"
                    route.save()
                    route.bookings.update(
                        driver=driver, hub=route.hub, status="assigned"
                    )
                self.message_user(
                    request, "Drivers assigned successfully.", level=messages.SUCCESS
                )
            except Exception as e:
                self.message_user(request, f"Error: {e}", level=messages.ERROR)
            return HttpResponseRedirect(".")

        # Form for selecting driver
        context = {
            "title": "Assign Driver to Selected Routes",
            "queryset": queryset,
            "opts": self.model._meta,
            "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
            "drivers": DriverProfile.objects.filter(status="active"),
        }
        return render(
            request, "admin/assign_driver.html", context
        )  # Create this template

    def re_optimize(self, request, queryset):
        # Re-run optimization on selected
        from .tasks import optimize_bookings

        optimize_bookings.delay()


@admin.register(Address)
class AddressAdmin(ModelAdmin):
    list_display = ("line1", "city", "region", "country", "validated")
    search_fields = ("line1", "city", "region", "postal_code")
    list_filter = ("city", "country", "validated")


@admin.register(Quote)
class QuoteAdmin(ModelAdmin):
    list_display = (
        "id",
        "get_service_type_name",
        "get_shipping_type_name",
        "weight_kg",
        "distance_km",
        "final_price",
        "created_at",
    )
    list_filter = ("service_type__name", "shipping_type__name", "created_at")
    search_fields = ("service_type__name", "shipping_type__name")
    readonly_fields = ("created_at",)

    def get_service_type_name(self, obj):
        return obj.service_type.name if obj.service_type else "N/A"

    get_service_type_name.short_description = "Service Type"

    def get_shipping_type_name(self, obj):
        return obj.shipping_type.name if obj.shipping_type else "N/A"

    get_shipping_type_name.short_description = "Shipping Type"


class PostcodeFilter(SimpleListFilter):
    """
    Custom admin filter for searching bookings by postcode
    (partial match in pickup or dropoff address).
    - Title: Appears in the sidebar as "Postcode".
    - Searches icontains in either pickup or dropoff postal_code.
    - Uses a custom template for an input field + submit button.
    """

    title = "postcode"  # Sidebar label (capitalized automatically)
    parameter_name = "postcode"  # Query param, e.g., ?postcode=00100

    # Custom template for the filter widget (input field)
    template = "admin/input_filter.html"

    def lookups(self, request, model_admin):
        # No fixed dropdown options; this is input-based
        return []

    def queryset(self, request, queryset):
        # Apply filter if value is provided
        postcode_value = self.value()
        if postcode_value:
            # Filter bookings where postcode matches (partial, case-insensitive) in EITHER address
            queryset = queryset.filter(
                models.Q(pickup_address__postal_code__icontains=postcode_value)
                | models.Q(dropoff_address__postal_code__icontains=postcode_value)
            )
        return queryset



# New: Inline for Refunds (nested in PaymentTransactionInline)
class RefundInline(admin.TabularInline):
    model = Refund
    extra = 0
    fields = ('amount', 'reason', 'status', 'created_at')
    readonly_fields = ('created_at',)
    can_delete = False  # Prevent accidental deletion
    verbose_name = "Refund"
    verbose_name_plural = "Refunds"



# New: Inline for PaymentTransactions (shown in Booking detail view)
class PaymentTransactionInline(admin.TabularInline):
    model = PaymentTransaction
    extra = 0
    fields = ('amount', 'status', 'reference', 'created_at')
    readonly_fields = ('reference', 'created_at')
    inlines = [RefundInline]  # Nest Refunds under Payments
    can_delete = False
    verbose_name = "Payment Transaction"
    verbose_name_plural = "Payment Transactions"


@admin.register(Booking)
class BookingAdmin(ModelAdmin):
    list_display = (
        "tracking_number",
        "get_customer_name",
        "get_hub",
        "get_driver_name",
        "status_badge",
        "get_service_type_name",
        "final_price",
        "created_at",
        "pod_link",
        # "refund_link", # Removed "refund_link" to move refund to detail view only
    )
    list_filter = (PostcodeFilter, "status", "quote__service_type__name", "created_at", "hub__name")
    search_fields = ("id", "customer__email", "customer__full_name", "guest_email", "pickup_address__postal_code",  # New
        "dropoff_address__postal_code", "tracking_number")
    readonly_fields = ("created_at", "updated_at", "refund_link")
    inlines = [PaymentTransactionInline]  # NEW: Added inline for payments/refunds in detail view

    ordering = ["-created_at"]  # List orders as latest descending

    def get_customer_name(self, obj):
        return (
            obj.customer.full_name if obj.customer else obj.guest_email or "Anonymous"
        )

    get_customer_name.short_description = "Customer"

    def get_service_type_name(self, obj):
        return (
            obj.quote.service_type.name
            if obj.quote and obj.quote.service_type
            else "N/A"
        )

    get_service_type_name.short_description = "Service Type"

    def get_driver_name(self, obj):
        return (
            obj.driver.user.full_name
            if obj.driver and obj.driver.user
            else "Unassigned"
        )

    get_driver_name.short_description = "Driver"

    # ------------------------------------------------------------------
    # Fixed get_hub – uses the real `hub` field (not route)
    # ------------------------------------------------------------------
    def get_hub(self, obj):
        # Most bookings have a hub directly (assigned when route is created)
        if obj.hub:
            return obj.hub.name
        # Fallback for very old bookings that might still have a route but no hub
        if hasattr(obj, "route_set") and obj.route_set.exists():
            first_route_hub = obj.route_set.first().hub
            return first_route_hub.name if first_route_hub else "-"
        return "Not assigned"

    get_hub.short_description = "Hub"
    get_hub.admin_order_field = "hub__name"  # ← enables sorting & filtering correctly

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
            "refunded": "#b91010",  # FIXED: Added for refunded status
        }.get(obj.status, "#444")
        return format_html(
            '<span style="padding:4px 8px;border-radius:9999px;color:#fff;background:{}">{}</span>',
            color,
            obj.get_status_display(),
        )

    status_badge.short_description = "Status"

    # Proof of Delivery View Button - Styled with blue theme for "info/view" action
    def pod_link(self, obj):
        # FIXED: Direct related_name (post-migration safe)
        pod = (
            obj.proof_of_delivery.first() if hasattr(obj, "proof_of_delivery") else None
        )
        if pod:
            url = reverse("admin:tracking_proofofdelivery_change", args=[pod.pk])
            return format_html(
                '<a href="{}" style="'
                "display: inline-block; "
                "padding: 6px 12px; "
                "margin: 2px; "
                "background-color: #3b82f6; "
                "color: white; "
                "text-decoration: none; "
                "border-radius: 4px; "
                "font-size: 12px; "
                "font-weight: 500; "
                "border: 1px solid #3b82f6; "
                "cursor: pointer; "
                '">View POD</a>',
                url,
            )
        return format_html('<span style="color: #6b7280; font-style: italic;">-</span>')

    pod_link.short_description = "POD"
    pod_link.allow_tags = True

    def refund_link(self, obj):
        # FIXED: Direct related_name (no fallback to avoid AttributeError)
        if hasattr(obj, "payment_transactions"):
            payment_qs = obj.payment_transactions
        else:
            payment_qs = None  # Graceful if not migrated
        if not payment_qs:
            return format_html(
                '<span style="color: #6b7280; font-style: italic;">No Payments</span>'
            )
        if obj.status in [BookingStatus.CANCELLED, BookingStatus.REFUNDED]:
            return format_html(
                '<span style="background-color: #10b981; color: white; padding: 4px 8px; border-radius: 4px;">Refunded</span>'
            )
        has_refund = payment_qs.filter(refunds__status="processed").exists()
        if has_refund:
            return format_html(
                '<span style="background-color: #10b981; color: white; padding: 4px 8px; border-radius: 4px;">Refunded</span>'
            )
        url = reverse("admin:bookings_booking_refund", args=[obj.pk])
        return format_html(
            '<a href="{}" style="background-color: #f59e0b; color: white; padding: 6px 12px; text-decoration: none; border-radius: 4px; font-weight: 500;">Offer Refund</a>',
            url,
        )

    refund_link.short_description = "Refund"
    refund_link.allow_tags = True

    @transaction.atomic
    def refund_booking_view(self, request, pk):
        booking = get_object_or_404(Booking, pk=pk)
        if booking.status in [BookingStatus.CANCELLED, BookingStatus.REFUNDED]:
            self.message_user(
                request, "Cannot refund finalized booking.", level="error"
            )
            return HttpResponseRedirect("..")

        # FIXED: Direct related_name with hasattr check
        if hasattr(booking, "payment_transactions"):
            payment_qs = booking.payment_transactions
        else:
            payment_qs = None
        if not payment_qs:
            self.message_user(
                request, "No payment relation found (check migration).", level="error"
            )
            return HttpResponseRedirect("..")
        tx = (
            payment_qs.filter(status=PaymentStatus.SUCCESS)
            .order_by("-created_at")
            .first()
        )
        if not tx:
            self.message_user(request, "No successful payment found.", level="error")
            return HttpResponseRedirect("..")

        # FIXED: Safe check for existing refund (OneToOne reverse accessor)
        has_refund = False
        try:
            refund = tx.refunds
            if refund.status == "processed":
                has_refund = True
        except Refund.DoesNotExist:
            pass
        if has_refund:
            self.message_user(request, "Already refunded.", level="warning")
            return HttpResponseRedirect("..")

        warning = ""
        if booking.status != BookingStatus.DELIVERED:
            warning = "Warning: This is not delivered—refunding will cancel the booking and notify driver/customer."

        if request.method == "POST":
            amount_str = request.POST.get("amount")
            reason = request.POST.get("reason", "").strip()[:255]
            try:
                amount = Decimal(amount_str)
                if amount <= 0 or amount != tx.amount:  # NEW: Enforce full
                    raise ValueError("Must be full refund amount.")
            except ValueError:
                self.message_user(
                    request, "Invalid amount—must match original.", level="error"
                )
                amount = None

            if amount and reason:
                # Duplicate check (redundant with serializer, but early UX)
                if Refund.objects.filter(transaction=tx).exists():
                    self.message_user(
                        request,
                        "A refund already exists for this transaction.",
                        level="error",
                    )
                    return HttpResponseRedirect("..")

                data = {"transaction": tx.id, "amount": amount, "reason": reason}
                serializer = RefundSerializer(data=data, context={"request": request})
                if serializer.is_valid():
                    try:
                        refund = serializer.save()  # Triggers full process
                        currency = getattr(tx, "currency", "GBP")
                        self.message_user(
                            request,
                            f"Full refund {amount} {currency} processed for booking {booking.id}. Customer notified.",
                            level="success",
                        )
                        return HttpResponseRedirect("..")
                    except serializers.ValidationError as e:
                        self.message_user(request, f"Error: {str(e)}", level="error")
                else:
                    self.message_user(
                        request,
                        f'Validation error: {list(serializer.errors.values())[0] if serializer.errors else "Unknown"}',
                        level="error",
                    )

        currency = getattr(tx, "currency", "GBP")
        context = {
            "title": f"Process Refund for Booking {booking.id} ({currency})",
            "booking": booking,
            "transaction": tx,
            "default_amount": tx.amount,  # Full tx amount pre-filled
            "warning": warning,
            "opts": self.model._meta,
            "original_url": reverse("admin:bookings_booking_changelist"),
        }
        return render(request, "admin/booking_refund_form.html", context)

    def get_urls(self):
        # FIXED: Single merged get_urls with correct namespaced names
        urls = super().get_urls()
        refund_view = self.admin_site.admin_view(self.refund_booking_view)
        custom_view = self.admin_site.admin_view(
            BulkAssignDriversView.as_view(model_admin=self)
        )

        custom_urls = [
            # FIXED: Full namespace for reverse
            path("<uuid:pk>/refund/", refund_view, name="bookings_booking_refund"),
            path(
                "bulk-assign-drivers/",
                custom_view,
                name="booking_booking_bulk_assign_drivers",
            ),
        ]
        return custom_urls + urls

    # Custom bulk action for assigning driver
    actions = ["assign_driver"]

    def assign_driver(self, request, queryset):
        class AssignDriverForm(forms.Form):
            driver = forms.ModelChoiceField(
                queryset=DriverProfile.objects.filter(status="active").order_by(
                    "user__full_name"
                ),
                label="Select Driver",
                required=True,
            )

        if "apply" in request.POST:
            form = AssignDriverForm(request.POST)
            if form.is_valid():
                driver = form.cleaned_data["driver"]
                # Only update bookings in 'scheduled' status and change to 'assigned'
                valid_statuses = ["scheduled"]
                valid_queryset = queryset.filter(status__in=valid_statuses)
                updated_count = valid_queryset.update(driver=driver, status="assigned")
                if updated_count > 0:
                    self.message_user(
                        request,
                        f"Assigned {driver.user.full_name} to {updated_count} booking{'s' if updated_count > 1 else ''} and updated status to 'Assigned'.",
                        level="success",
                    )
                else:
                    self.message_user(
                        request,
                        "No bookings were updated. Ensure selected bookings are in 'scheduled' status.",
                        level="warning",
                    )
                return HttpResponseRedirect(".")
            else:
                self.message_user(request, "Invalid driver selection.", level="error")
        else:
            form = AssignDriverForm()

        return render(
            request,
            "admin/assign_driver_intermediate.html",
            {
                "title": "Assign Driver to Selected Bookings",
                "bookings": queryset,
                "form": form,
                "opts": self.model._meta,
                "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
            },
        )

    assign_driver.short_description = (
        "Assign driver to selected bookings (Scheduled only)"
    )


class BulkAssignDriversView(UnfoldModelAdminViewMixin, TemplateView):
    title = "Bulk Assign Drivers"
    permission_required = ("bookings.change_booking",)
    template_name = "admin/bulk_assign_drivers.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        bookings = Booking.objects.filter(status="scheduled").order_by("-created_at")
        paginator = Paginator(bookings, 15)  # 25 bookings per page
        page_number = self.request.GET.get("page")
        page_obj = paginator.get_page(page_number)
        context["drivers"] = DriverProfile.objects.filter(status="active").order_by(
            "user__full_name"
        )
        context["bookings"] = page_obj  # Pass page_obj as bookings
        context["opts"] = Booking._meta
        return context

    def post(self, request, *args, **kwargs):
        driver_id = request.POST.get("driver")
        # Get list of selected booking IDs
        booking_ids = request.POST.getlist("booking_ids")
        try:
            driver = DriverProfile.objects.get(id=driver_id, status="active")
            bookings = Booking.objects.filter(id__in=booking_ids, status="scheduled")
            updated_count = bookings.update(driver=driver, status="assigned")
            messages.success(
                request,
                f"Assigned {driver.user.full_name} to {updated_count} booking{'s' if updated_count > 1 else ''} and updated status to 'Assigned'.",
            )
        except DriverProfile.DoesNotExist:
            messages.error(request, "Selected driver does not exist or is not active.")
        except Exception as e:
            messages.error(request, f"Error: {str(e)}")
        return HttpResponseRedirect(".")


@admin.register(ShippingType)
class ShippingTypeAdmin(ModelAdmin):
    list_display = ("id", "name", "description", "created_at", "updated_at")
    search_fields = ("name", "description")
    list_filter = ("created_at", "updated_at")


@admin.register(ServiceType)
class ServiceTypeAdmin(ModelAdmin):
    list_display = ("name", "created_at", "description", "updated_at", "urgency_multiplier", "minimum_price")
    list_editable = ("urgency_multiplier", "minimum_price")
    search_fields = ("name", "description")
    fieldsets = (
        (None, {
            'fields': ('name', 'description')
        }),
        ('Pricing Controls', {
            'fields': ('urgency_multiplier', 'minimum_price', 'legacy_price'),
            'description': "Controls how much more expensive this service is compared to standard tier pricing."
        }),
    )


@admin.register(RecurringSchedule)
class RecurringScheduleAdmin(ModelAdmin):
    list_display = (
        "id",
        "customer",
        "recurrence",
        "next_run_at",
        "active",
        "created_at",
    )
    list_filter = ("recurrence", "active", "created_at")
    search_fields = ("customer__email", "customer__full_name")


@admin.register(BulkUpload)
class BulkUploadAdmin(ModelAdmin):
    list_display = ("id", "customer", "created_at", "processed")
    list_filter = ("processed", "created_at")
    search_fields = ("customer__email", "customer__full_name")
