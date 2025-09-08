from django.contrib import admin
from django.utils.html import format_html
from unfold.admin import ModelAdmin
from .models import Address, Quote, Booking, ShippingType, ServiceType, RecurringSchedule, BulkUpload
from django import forms
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import path
from django.contrib import messages
from driver.models import DriverProfile
from unfold.views import UnfoldModelAdminViewMixin
from django.views.generic import TemplateView
import uuid
from django.core.paginator import Paginator


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
    list_display = ("id", "get_customer_name", "get_service_type_name","get_driver_name",
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

    def get_driver_name(self, obj):
        return obj.driver.user.full_name if obj.driver and obj.driver.user else "Unassigned"
    get_driver_name.short_description = "Driver"

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

    # Custom bulk action for assigning driver
    actions = ["assign_driver"]

    def assign_driver(self, request, queryset):
        class AssignDriverForm(forms.Form):
            driver = forms.ModelChoiceField(
                queryset=DriverProfile.objects.filter(status="active").order_by("user__full_name"),
                label="Select Driver",
                required=True
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
                        level="success"
                    )
                else:
                    self.message_user(
                        request,
                        "No bookings were updated. Ensure selected bookings are in 'scheduled' status.",
                        level="warning"
                    )
                return HttpResponseRedirect(".")
            else:
                self.message_user(request, "Invalid driver selection.", level="error")
        else:
            form = AssignDriverForm()

        return render(request, "admin/assign_driver_intermediate.html", {
            "title": "Assign Driver to Selected Bookings",
            "bookings": queryset,
            "form": form,
            "opts": self.model._meta,
            "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
        })

    assign_driver.short_description = "Assign driver to selected bookings (Scheduled only)"

    def get_urls(self):
        urls = super().get_urls()
        custom_view = self.admin_site.admin_view(
            BulkAssignDriversView.as_view(model_admin=self)
        )
        custom_urls = [
            path("bulk-assign-drivers/", custom_view, name="booking_booking_bulk_assign_drivers"),
        ]
        return custom_urls + urls


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
        context["drivers"] = DriverProfile.objects.filter(status="active").order_by("user__full_name")
        context["bookings"] = page_obj  # Pass page_obj as bookings
        context["opts"] = Booking._meta
        return context

    def post(self, request, *args, **kwargs):
        driver_id = request.POST.get("driver")
        booking_ids = request.POST.getlist("booking_ids")  # Get list of selected booking IDs
        try:
            driver = DriverProfile.objects.get(id=driver_id, status="active")
            bookings = Booking.objects.filter(id__in=booking_ids, status="scheduled")
            updated_count = bookings.update(driver=driver, status="assigned")
            messages.success(
                request,
                f"Assigned {driver.user.full_name} to {updated_count} booking{'s' if updated_count > 1 else ''} and updated status to 'Assigned'."
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
