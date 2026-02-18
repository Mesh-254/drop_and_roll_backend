# bookings/admin/actions.py
"""
Admin actions / intermediate views extracted from model admin classes.
Kept separate to avoid bloating admin.py.
"""

import logging
from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from bookings.models import BookingStatus, Hub, Route
from bookings.utils.distance_utils import get_time_matrix
from bookings.utils.route_optimization import optimize_route_single
from driver.models import DriverProfile, DriverShift

logger = logging.getLogger(__name__)


def create_route_from_selected(admin_instance, request, queryset):
    """
    Admin action: Create a manual route from selected bookings.
    Extracted from BookingAdmin to keep the model admin class clean.
    """

    class CreateRouteForm(forms.Form):
        hub = forms.ModelChoiceField(
            queryset=Hub.objects.all(), required=False, label="Hub (override if needed)"
        )
        leg_type = forms.ChoiceField(
            choices=[
                ("pickup", "Pickup"),
                ("delivery", "Delivery"),
            ]
            + (
                [("mixed", "Mixed")] if getattr(settings, "MIXED_ROUTES", False) else []
            ),
            required=True,
            label="Route Type",
        )
        driver = forms.ModelChoiceField(
            queryset=DriverProfile.objects.filter(status="active").order_by(
                "user__full_name"
            ),
            required=False,
            label="Assign Driver (optional)",
        )

    # Show form
    if "apply" not in request.POST:
        form = CreateRouteForm()
        return render(
            request,
            "admin/create_route_intermediate.html",
            {
                "title": "Create Manual Route from Selected Bookings",
                "bookings": queryset,
                "form": form,
                "opts": admin_instance.model._meta,
                "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
            },
        )

    form = CreateRouteForm(request.POST)
    if not form.is_valid():
        admin_instance.message_user(request, "Invalid form data.", level=messages.ERROR)
        return HttpResponseRedirect(request.get_full_path())

    cleaned = form.cleaned_data
    hub = cleaned["hub"]
    leg_type = cleaned["leg_type"]
    driver = cleaned["driver"]

    # ─── Hub consistency check ───────────────────────────────────────
    booking_hubs = {b.hub_id for b in queryset if b.hub_id}
    if len(booking_hubs) > 1:
        admin_instance.message_user(
            request,
            "Selected bookings belong to multiple hubs. Please select bookings from one hub.",
            level=messages.ERROR,
        )
        return HttpResponseRedirect(request.get_full_path())

    if booking_hubs:
        hub = Hub.objects.get(id=next(iter(booking_hubs)))

    if not hub:
        admin_instance.message_user(
            request, "No hub could be determined.", level=messages.ERROR
        )
        return HttpResponseRedirect(request.get_full_path())

    # ─── Determine stop types ────────────────────────────────────────
    stop_types = []
    invalid_statuses = []

    for booking in queryset:
        if leg_type == "mixed":
            if booking.status in [BookingStatus.SCHEDULED, BookingStatus.ASSIGNED]:
                stop_types.append("pickup")
            elif booking.status in [BookingStatus.AT_HUB, BookingStatus.IN_TRANSIT]:
                stop_types.append("delivery")
            else:
                invalid_statuses.append(str(booking.id))
        else:
            stop_types.append(leg_type)

    if invalid_statuses:
        admin_instance.message_user(
            request,
            f"Invalid status for mixed route (bookings: {', '.join(invalid_statuses)})",
            level=messages.ERROR,
        )
        return HttpResponseRedirect(request.get_full_path())

    # ─── Collect valid addresses ─────────────────────────────────────
    addresses = []
    invalid_coords = []

    for booking, stop_type in zip(queryset, stop_types):
        addr = (
            booking.pickup_address if stop_type == "pickup" else booking.dropoff_address
        )
        if addr and addr.latitude is not None and addr.longitude is not None:
            addresses.append(addr)
        else:
            invalid_coords.append(str(booking.id))

    if invalid_coords:
        admin_instance.message_user(
            request,
            f"Some bookings lack coordinates: {', '.join(invalid_coords)}",
            level=messages.WARNING,
        )
        if len(invalid_coords) == len(queryset):
            return HttpResponseRedirect(request.get_full_path())

    # ─── Matrix calculation ──────────────────────────────────────────
    hub_lat = float(hub.address.latitude) if hub.address.latitude else None
    hub_lng = float(hub.address.longitude) if hub.address.longitude else None

    if hub_lat is None or hub_lng is None:
        admin_instance.message_user(
            request, "Hub missing coordinates.", level=messages.ERROR
        )
        return HttpResponseRedirect(request.get_full_path())

    time_matrix, distance_matrix = get_time_matrix(
        addresses, hub_lat=hub_lat, hub_lng=hub_lng
    )

    # ─── Optimize route ──────────────────────────────────────────────
    try:
        ordered, hrs, km, _, etas = optimize_route_single(
            list(queryset),
            time_matrix,
            distance_matrix,
            driver=driver,
            time_windows=None,
            stop_types=stop_types,
            leg_type=leg_type,
        )
    except Exception as exc:
        logger.exception("Manual route optimization failed")
        admin_instance.message_user(
            request, f"Optimization failed: {exc}", level=messages.ERROR
        )
        return HttpResponseRedirect(request.get_full_path())

    # ─── Build ordered stops ─────────────────────────────────────────
    ordered_stops = []
    original_qs_list = list(queryset)

    for i, booking in enumerate(ordered):
        orig_idx = original_qs_list.index(booking)
        stop_type = stop_types[orig_idx]
        addr = (
            booking.pickup_address if stop_type == "pickup" else booking.dropoff_address
        )

        ordered_stops.append(
            {
                "booking_id": str(booking.id),
                "tracking_number": booking.tracking_number or str(booking.id)[:8],
                "type": stop_type,
                "status": booking.status,
                "lat": float(addr.latitude) if addr.latitude else None,
                "lng": float(addr.longitude) if addr.longitude else None,
                "address_short": f"{addr.line1[:30]}... {addr.postal_code or ''}".strip(),
                "eta": etas[i].isoformat() if etas and i < len(etas) else None,
            }
        )

    now = timezone.now()

    # ─── Atomic creation ─────────────────────────────────────────────
    try:
        with transaction.atomic():
            if driver:
                shift = DriverShift.get_or_create_today(driver)
                current = shift.current_load or {
                    "weight": 0.0,
                    "volume": 0.0,
                    "hours": 0.0,
                }
                projected = current["hours"] + hrs
                max_hours = getattr(settings, "MAX_DAILY_HOURS", 10.0)
                if projected > max_hours:
                    raise ValueError(
                        f"Exceeds max daily hours ({projected:.1f}/{max_hours}h)"
                    )
                current["hours"] = round(projected, 2)
                shift.current_load = current
                shift.status = DriverShift.Status.ASSIGNED
                shift.save(update_fields=["current_load", "status"])
            else:
                shift = DriverShift.objects.create(
                    driver=None,
                    start_time=now.replace(hour=8, minute=0, second=0, microsecond=0),
                    end_time=now.replace(hour=18, minute=0, second=0, microsecond=0),
                    status=DriverShift.Status.PENDING,
                    current_load={"weight": 0.0, "volume": 0.0, "hours": 0.0},
                )

            route = Route.objects.create(
                driver=driver,
                shift=shift,
                leg_type=leg_type,
                ordered_stops=ordered_stops,
                total_time_hours=round(hrs, 3),
                total_distance_km=round(km, 3),
                status="assigned" if driver else "pending",
                visible_at=now,
                hub=hub,
            )
            route.bookings.set(ordered)
            route.save()

            # Update bookings
            for booking in ordered:
                orig_idx = original_qs_list.index(booking)
                stop_type = stop_types[orig_idx]
                new_status = (
                    BookingStatus.ASSIGNED
                    if stop_type == "delivery"
                    else BookingStatus.IN_TRANSIT
                )
                booking.driver = driver
                booking.hub = hub
                booking.status = new_status
                booking.updated_at = now
                booking.save(update_fields=["driver", "hub", "status", "updated_at"])

        admin_instance.message_user(
            request,
            f"Route #{route.id} created successfully ({len(ordered)} bookings).",
            level=messages.SUCCESS,
        )
        return HttpResponseRedirect(
            reverse("admin:bookings_route_change", args=(route.id,))
        )

    except ValueError as ve:
        admin_instance.message_user(request, str(ve), level=messages.ERROR)
    except Exception as exc:
        logger.exception("Failed to create manual route")
        admin_instance.message_user(request, f"Error: {exc}", level=messages.ERROR)

    return HttpResponseRedirect(request.get_full_path())


create_route_from_selected.short_description = (
    "Create route from selected bookings (manual / same-day)"
)
