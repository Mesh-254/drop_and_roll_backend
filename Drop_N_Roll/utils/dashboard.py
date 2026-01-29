import json
from datetime import timedelta
from django.db.models import Sum, Count, Avg, Q
from datetime import timedelta, datetime
from django.db.models import Sum, Count, Avg, Q, F
from django.utils import timezone
from django.contrib.auth import get_user_model
from bookings.models import (
    Booking,
    Quote,
    ServiceType,
    ShippingType,
    RecurringSchedule,
    BulkUpload,
    Address,
    Route,
    Hub,
    BookingStatus,
)
from driver.models import DriverProfile, DriverPayout, DriverRating, DriverInvitation
from payments.models import PaymentTransaction, Refund, Wallet
from support.models import Ticket, TicketStatus

User = get_user_model()


def dashboard_callback(request, context):
    """
    Dashboard callback for Unfold admin panel.
    Populates context with KPIs, charts, and tables.
    Supports date range filtering for relevant metrics.
    Restricted to superusers.
    """
    if not request.user.is_superuser:
        context.update({"error": "Access restricted to superusers."})
        return context

    # ==================== Date Range Filtering ====================
    today = timezone.now().date()
    from_date_str = request.GET.get("from_date")
    to_date_str = request.GET.get("to_date")
    from_date = None
    to_date = None
    date_error = None

    if from_date_str:
        try:
            from_date = datetime.strptime(from_date_str, "%Y-%m-%d").date()
        except ValueError:
            date_error = 'Invalid "from" date. Use YYYY-MM-DD.'

    if to_date_str:
        try:
            to_date = datetime.strptime(to_date_str, "%Y-%m-%d").date()
        except ValueError:
            date_error = 'Invalid "to" date. Use YYYY-MM-DD.'

    if not date_error and from_date and to_date and from_date > to_date:
        date_error = '"From" date cannot be after "To" date.'

    has_date_range = bool(not date_error and (from_date or to_date))

    date_filter = Q()
    if from_date:
        date_filter &= Q(created_at__date__gte=from_date)
    if to_date:
        date_filter &= Q(created_at__date__lte=to_date)

    # ==================== Apply Filtered querysets ====================
    bookings_qs = (
        Booking.objects.filter(date_filter) if has_date_range else Booking.objects.all()
    )
    quotes_qs = (
        Quote.objects.filter(date_filter) if has_date_range else Quote.objects.all()
    )
    payout_filter = date_filter
    payout_qs = (
        DriverPayout.objects.filter(payout_filter)
        if has_date_range
        else DriverPayout.objects.all()
    )

    # ==================== User KPIs (not date-dependent) ====================
    total_users = User.objects.count()
    users_by_role = list(User.objects.values("role").annotate(count=Count("id")))
    total_customers = next(
        (item["count"] for item in users_by_role if item["role"] == "customer"), 0
    )
    total_drivers = next(
        (item["count"] for item in users_by_role if item["role"] == "driver"), 0
    )
    total_admins = next(
        (item["count"] for item in users_by_role if item["role"] == "admin"), 0
    )

    # ==================== Booking KPIs ====================

    total_bookings = Booking.objects.count()
    bookings_by_status = list(bookings_qs.values("status").annotate(count=Count("id")))
    booking_status_counts = {
        item["status"]: item["count"] for item in bookings_by_status
    }
    total_quotes = quotes_qs.count()
    total_service_types = ServiceType.objects.count()
    total_shipping_types = ShippingType.objects.count()
    average_booking_value = (
        Booking.objects.aggregate(avg=Avg("final_price"))["avg"] or 0
    )
    total_recurring_schedules = RecurringSchedule.objects.filter(active=True).count()
    total_bulk_uploads = BulkUpload.objects.count()
    total_processed_bulk_uploads = BulkUpload.objects.filter(processed=True).count()
    total_validated_addresses = Address.objects.filter(validated=True).count()
    total_failed_bookings = Booking.objects.filter(status="failed").count()
    total_cancelled_bookings = Booking.objects.filter(status="cancelled").count()

    # ==================== Driver KPIs ====================
    drivers_by_status = list(
        DriverProfile.objects.values("status")
        .annotate(count=Count("id"))
        .order_by("status")
    )
    driver_status_counts = {item["status"]: item["count"] for item in drivers_by_status}
    total_payouts = payout_qs.aggregate(total=Sum("amount"))["total"] or 0
    total_driver_invitations = DriverInvitation.objects.count()
    pending_driver_invitations = DriverInvitation.objects.filter(
        status=DriverInvitation.Status.PENDING
    ).count()
    average_driver_rating = (
        DriverRating.objects.aggregate(avg=Avg("rating"))["avg"] or 0
    )

    # ==================== Payment KPIs ====================

    total_successful_payments = PaymentTransaction.objects.filter(
        status="success"
    ).count()
    total_payments = PaymentTransaction.objects.count()
    payment_success_rate = round(
        (total_successful_payments / total_payments * 100) if total_payments > 0 else 0,
        2,
    )
    total_revenue = (
        PaymentTransaction.objects.filter(
            status="success",  # or 'completed', 'paid', etc.
            booking__isnull=False,
            booking__created_at__date__range=(
                (from_date, to_date) if has_date_range else Q()
            ),  # reuse your date filter logic
        ).aggregate(total=Sum("amount"))["total"]
        or 0
    )
    average_booking_value = bookings_qs.aggregate(avg=Avg("final_price"))["avg"] or 0
    total_refunds = Refund.objects.aggregate(total=Sum("amount"))["total"] or 0
    total_wallet_balance = Wallet.objects.aggregate(total=Sum("balance"))["total"] or 0
    total_loyalty_points = (
        User.objects.filter(role="customer").aggregate(total=Sum("loyalty_points"))[
            "total"
        ]
        or 0
    )

    # ==================== Service Type KPIs ====================
    top_service_type = (
        ServiceType.objects.annotate(usage=Count("quotes__bookings"))
        .order_by("-usage")
        .first()
    )
    top_service_type_name = top_service_type.name if top_service_type else "N/A"
    top_service_type_usage = top_service_type.usage if top_service_type else 0

    # ==================== Support KPIs ====================
    total_tickets = Ticket.objects.count()
    open_tickets = Ticket.objects.filter(status=TicketStatus.OPEN).count()
    complaints = Ticket.objects.filter(type="complaint").count()
    average_resolution_time = Ticket.objects.filter(
        status=TicketStatus.RESOLVED
    ).aggregate(avg=Avg(F("updated_at") - F("created_at")))["avg"] or timedelta(0)
    average_resolution_time_str = (
        f"{average_resolution_time.days} days, {average_resolution_time.seconds // 3600} hours"
        if average_resolution_time
        else "N/A"
    )

    recent_open_tickets = list(
        Ticket.objects.filter(status=TicketStatus.OPEN)
        .order_by("-created_at")[:10]
        .values(
            "id",
            "subject",
            "status",
            "priority",
            "created_at",
            "user__email",
            "guest_email",
        )
    )
    recent_open_tickets_table = [
        {
            "id": str(ticket["id"])[:8],
            "subject": ticket["subject"],
            "user": ticket["user__email"] or ticket["guest_email"],
            "priority": ticket["priority"],
            "status": ticket["status"],
            "created_at": ticket["created_at"].strftime("%Y-%m-%d %H:%M"),
        }
        for ticket in recent_open_tickets
    ]

    # ********************************************************************************
    # Add Route/Hub/Driver KPIs
    # ********************************************************************************

    total_routes = Route.objects.count()
    routes_by_status = Route.objects.values("status").annotate(count=Count("id"))
    route_status_counts = {item["status"]: item["count"] for item in routes_by_status}
    # NEW: Hub KPIs (total hubs, total active/completed across hubs)
    total_hubs = Hub.objects.count()

    total_active_bookings = Booking.objects.filter(
        status__in=[
            BookingStatus.SCHEDULED,
            BookingStatus.ASSIGNED,
            BookingStatus.PICKED_UP,
            BookingStatus.AT_HUB,
            BookingStatus.IN_TRANSIT,
        ]
    ).count()
    total_completed_bookings = Booking.objects.filter(
        status=BookingStatus.DELIVERED
    ).count()

    # NEW: Enhanced hub metrics table data (with routes and drivers)
    hub_metrics = Hub.objects.annotate(
        active_bookings=Count(
            "hub_bookings",
            filter=Q(
                hub_bookings__status__in=[
                    BookingStatus.SCHEDULED,
                    BookingStatus.ASSIGNED,
                    BookingStatus.PICKED_UP,
                    BookingStatus.AT_HUB,
                    BookingStatus.IN_TRANSIT,
                ]
            ),
        ),
        completed_bookings=Count(
            "hub_bookings", filter=Q(hub_bookings__status=BookingStatus.DELIVERED)
        ),
        routes_count=Count("routes"),
        active_drivers=Count("drivers", filter=Q(drivers__status="active")),
    ).values(
        "name",
        "active_bookings",
        "completed_bookings",
        "routes_count",
        "active_drivers",
    )

    # NEW: Hub chart (bar chart for active/completed per hub)
    hub_labels = [item["name"] for item in hub_metrics]
    active_data = [item["active_bookings"] for item in hub_metrics]
    completed_data = [item["completed_bookings"] for item in hub_metrics]
    hub_chart_data = json.dumps(
        {
            "type": "bar",
            "labels": hub_labels,
            "datasets": [
                {
                    "label": "Active Bookings",
                    "data": active_data,
                    "backgroundColor": "#3b82f6",
                },
                {
                    "label": "Completed Bookings",
                    "data": completed_data,
                    "backgroundColor": "#10b981",
                },
            ],
        }
    )

    # NEW: Pending routes overview (to help assignment) - List pending routes with hub and suggested drivers
    pending_routes = (
        Route.objects.filter(status="pending")
        .select_related("hub")
        .annotate(bookings_count=Count("bookings"))
        .values("id", "hub__name", "leg_type", "total_time_hours", "bookings_count")
    )

    pending_routes_with_suggestions = []
    for pr in pending_routes:
        if pr["hub__name"]:
            # Suggest top 3 available drivers from same hub (similar to API logic)
            suggested_drivers = (
                DriverProfile.objects.filter(hub__name=pr["hub__name"], status="active")
                .select_related("user")[:3]
                .values("user__full_name")
            )
            suggested_drivers = list(suggested_drivers)
        pr["suggested_drivers"] = suggested_drivers
        pending_routes_with_suggestions.append(pr)

    total_assigned_jobs = Booking.objects.filter(status="assigned").count()
    drivers_with_jobs = (
        DriverProfile.objects.annotate(job_count=Count("bookings"))
        .filter(job_count__gt=0)
        .count()
    )  # FIXED HERE

    # Recent Routes Table (optimized with prefetch)
    recent_routes = Route.objects.prefetch_related("bookings", "driver").order_by(
        "-id"
    )[:5]
    recent_routes_table = [
        {
            "id": route.id,
            "driver": route.driver.user.full_name if route.driver else "Unassigned",
            "leg_type": route.leg_type,
            "status": route.status,
            "bookings": route.bookings.count(),
            "total_time": (
                f"{route.total_time_hours:.2f} hours"
                if route.total_time_hours
                else "N/A"
            ),
        }
        for route in recent_routes
    ]

    # Route Status Pie Chart
    route_chart_data = json.dumps(
        {
            "type": "pie",
            "labels": list(route_status_counts.keys()) or ["No Data"],
            "datasets": [
                {
                    "data": list(route_status_counts.values()) or [0],
                    "backgroundColor": [
                        "#FF6384",
                        "#36A2EB",
                        "#FFCE56",
                        "#4BC0C0",
                        "#9966FF",
                    ],
                }
            ],
        }
    )

    # Chart Data: Bookings over the last 7 days
    today = timezone.now().date()
    bookings_data = []
    revenue_data = []
    payout_data = []
    for i in range(7):
        day = today - timedelta(days=i)
        count = Booking.objects.filter(created_at__date=day).count()
        revenue = (
            Booking.objects.filter(created_at__date=day).aggregate(
                total=Sum("final_price")
            )["total"]
            or 0
        )
        payouts = (
            DriverPayout.objects.filter(created_at__date=day).aggregate(
                total=Sum("amount")
            )["total"]
            or 0
        )
        bookings_data.append({"date": day.strftime("%Y-%m-%d"), "count": count})
        revenue_data.append({"date": day.strftime("%Y-%m-%d"), "revenue": revenue})
        payout_data.append({"date": day.strftime("%Y-%m-%d"), "payout": payouts})
    # Payments
    payment_qs = PaymentTransaction.objects.all()
    if has_date_range:
        payment_qs = payment_qs.filter(date_filter)
    total_successful_payments = payment_qs.filter(status="success").count()
    total_payments = payment_qs.count()
    payment_success_rate = (
        round(total_successful_payments / total_payments * 100, 2)
        if total_payments
        else 0
    )

    average_driver_rating = (
        DriverRating.objects.aggregate(avg=Avg("rating"))["avg"] or 0
    )

    # ==================== TOP SERVICE TYPE (FIXED WITH F()) ====================
    if has_date_range:
        top = (
            bookings_qs.values(service_type_name=F("quote__service_type__name"))
            .annotate(usage=Count("id"))
            .order_by("-usage")
            .first()
        )
    else:
        top = (
            Booking.objects.values(service_type_name=F("quote__service_type__name"))
            .annotate(usage=Count("id"))
            .order_by("-usage")
            .first()
        )

    top_service_type_name = top["service_type_name"] if top else "N/A"
    top_service_type_usage = top["usage"] if top else 0

    total_failed_bookings = bookings_qs.filter(status="failed").count()
    total_cancelled_bookings = bookings_qs.filter(status="cancelled").count()

    # ==================== CHARTS: DAILY DATA ====================
    chart_start = from_date or (today - timedelta(days=29))
    chart_end = to_date or today
    date_range = []
    current = chart_start
    while current <= chart_end:
        date_range.append(current)
        current += timedelta(days=1)

    bookings_daily = []
    revenue_daily = []
    payout_daily = []

    for day in date_range:
        day_str = day.strftime("%Y-%m-%d")
        day_filter = Q(created_at__date=day)
        bookings_daily.append(
            {"date": day_str, "count": bookings_qs.filter(day_filter).count()}
        )
        revenue_daily.append(
            {
                "date": day_str,
                "revenue": float(
                    bookings_qs.filter(day_filter).aggregate(total=Sum("final_price"))[
                        "total"
                    ]
                    or 0
                ),
            }
        )
        payout_daily.append(
            {
                "date": day_str,
                "payout": float(
                    payout_qs.filter(day_filter).aggregate(total=Sum("amount"))["total"]
                    or 0
                ),
            }
        )

    # Bookings Line Chart (smooth, professional)
    chart_data = json.dumps(
        {
            "type": "line",
            "labels": [d["date"] for d in bookings_daily],
            "datasets": [
                {
                    "label": "Bookings",
                    "data": [d["count"] for d in bookings_daily],
                    "borderColor": "rgb(168, 85, 247)",
                    "backgroundColor": "rgba(168, 85, 247, 0.2)",
                    "fill": True,
                    "tension": 0.4,  # Smooth curves
                    "borderWidth": 2,
                    "pointRadius": 0,  # Hide points for cleaner look
                }
            ],
        }
    )

    # Revenue Line Chart
    revenue_chart_data = json.dumps(
        {
            "type": "line",
            "labels": [d["date"] for d in revenue_daily],
            "datasets": [
                {
                    "label": "Revenue",
                    "data": [d["revenue"] for d in revenue_daily],
                    "borderColor": "rgb(34, 197, 94)",
                    "backgroundColor": "rgba(34, 197, 94, 0.2)",
                    "fill": True,
                    "tension": 0.4,
                    "borderWidth": 2,
                    "pointRadius": 0,
                }
            ],
        }
    )

    # Payouts Line Chart
    payout_chart_data = json.dumps(
        {
            "type": "line",
            "labels": [d["date"] for d in payout_daily],
            "datasets": [
                {
                    "label": "Payouts",
                    "data": [d["payout"] for d in payout_daily],
                    "borderColor": "rgb(59, 130, 246)",
                    "backgroundColor": "rgba(59, 130, 246, 0.2)",
                    "fill": True,
                    "tension": 0.4,
                    "borderWidth": 2,
                    "pointRadius": 0,
                }
            ],
        }
    )

    # Booking Status Pie Chart (enhanced colors)
    status_labels = list(booking_status_counts.keys()) or ["No Data"]
    status_data = list(booking_status_counts.values()) or [0]
    status_chart_data = json.dumps(
        {
            "type": "pie",
            "labels": status_labels,
            "datasets": [
                {
                    "data": status_data,
                    "backgroundColor": [
                        "#999999",
                        "#0ea5e9",
                        "#f59e0b",
                        "#6366f1",
                        "#06b6d4",
                        "#16a34a",
                        "#ef4444",
                        "#b91c1c",
                        "#a78bfa",
                        "#ec4899",
                    ],
                }
            ],
        }
    )

    # User Roles Pie Chart
    role_labels = [item["role"] for item in users_by_role] or ["No Data"]
    role_data = [item["count"] for item in users_by_role] or [0]
    role_chart_data = json.dumps(
        {
            "type": "pie",
            "labels": role_labels,
            "datasets": [
                {
                    "data": role_data,
                    "backgroundColor": ["#f59e0b", "#0ea5e9", "#16a34a", "#ef4444"],
                }
            ],
        }
    )

    # Chart: Bookings by Service Type
    service_type_usage = (
        ServiceType.objects.annotate(usage=Count("quotes__bookings"))
        .values("name", "usage")
        .order_by("-usage")
    )
    service_type_usage = (
        ServiceType.objects.annotate(usage=Count("quotes__bookings"))
        .values("name", "usage")
        .order_by("-usage")
    )
    service_labels = [item["name"] for item in service_type_usage]
    service_data = [item["usage"] for item in service_type_usage]

    # Service Type Bar Chart (multi-color bars for design)
    service_chart_data = json.dumps(
        {
            "type": "bar",
            "labels": service_labels or ["No Data"],
            "datasets": [
                {
                    "label": "Bookings",
                    "data": service_data or [0],
                    "backgroundColor": [
                        "rgb(168, 85, 247)",
                        "rgb(139, 92, 246)",
                        "rgb(124, 58, 237)",
                        "rgb(99, 102, 241)",
                        "rgb(79, 70, 229)",
                    ],  # Gradient-like purples
                }
            ],
        }
    )

    # Chart: Ratings Distribution
    ratings_distribution = (
        DriverRating.objects.values("rating")
        .annotate(count=Count("id"))
        .order_by("rating")
    )
    ratings_distribution = (
        DriverRating.objects.values("rating")
        .annotate(count=Count("id"))
        .order_by("rating")
    )
    ratings_labels = [str(item["rating"]) for item in ratings_distribution]
    ratings_data = [item["count"] for item in ratings_distribution]

    # Ratings Bar Chart
    ratings_chart_data = json.dumps(
        {
            "type": "bar",
            "labels": ratings_labels or ["No Data"],
            "datasets": [
                {
                    "label": "Ratings",
                    "data": ratings_data or [0],
                    "backgroundColor": "rgb(234, 179, 8)",
                }
            ],
        }
    )

    # Chart: Payments by Method
    payment_methods = (
        PaymentTransaction.objects.values("method__method_type")
        .annotate(count=Count("id"))
        .order_by("method__method_type")
    )
    payment_method_labels = [item["method__method_type"] for item in payment_methods]
    payment_methods = (
        PaymentTransaction.objects.values("method__method_type")
        .annotate(count=Count("id"))
        .order_by("method__method_type")
    )
    payment_method_labels = [
        item["method__method_type"] or "Unknown" for item in payment_methods
    ]
    payment_method_data = [item["count"] for item in payment_methods]

    # Payment Methods Pie Chart (enhanced colors)
    payment_method_chart_data = json.dumps(
        {
            "type": "pie",
            "labels": payment_method_labels or ["No Data"],
            "datasets": [
                {
                    "data": payment_method_data or [0],
                    "backgroundColor": [
                        "#ef4444",
                        "#3b82f6",
                        "#10b981",
                        "#f59e0b",
                        "#6b7280",
                        "#6366f1",
                        "#a78bfa",
                        "#ec4899",
                    ],
                }
            ],
        }
    )

    # ==================== RECENT BOOKINGS ====================
    recent_qs = bookings_qs.select_related(
        "customer", "quote", "quote__service_type"
    ).order_by("-created_at")[:10]
    if not has_date_range:
        recent_qs = Booking.objects.select_related(
            "customer", "quote", "quote__service_type"
        ).order_by("-created_at")[:10]

    recent_bookings = recent_qs.values(
        "id",
        "status",
        "final_price",
        "created_at",
        "customer__full_name",
        "quote__service_type__name",
    )

    recent_bookings_table = [
        {
            "id": str(b["id"])[:8],
            "customer": b["customer__full_name"] or "Guest",
            "service_type": b["quote__service_type__name"] or "Unknown",
            "status": b["status"],
            "final_price": f"GBP {b['final_price']}",
            "created_at": b["created_at"].strftime("%Y-%m-%d %H:%M"),
        }
        for b in recent_bookings
    ]

    # ==================== UPDATE CONTEXT ====================
    context.update(
        {
            "date_from": request.GET.get("from_date", ""),
            "date_to": request.GET.get("to_date", ""),
            "date_error": date_error,
            "total_users": total_users,
            "total_customers": total_customers,
            "total_drivers": total_drivers,
            "total_admins": total_admins,
            "total_bookings": total_bookings,
            "booking_status_counts": booking_status_counts,
            "total_quotes": total_quotes,
            "total_service_types": total_service_types,
            "total_shipping_types": total_shipping_types,
            "total_revenue": total_revenue,
            "average_booking_value": average_booking_value,
            "total_recurring_schedules": total_recurring_schedules,
            "driver_status_counts": driver_status_counts,
            "total_payouts": total_payouts,
            "total_refunds": total_refunds,
            "total_wallet_balance": total_wallet_balance,
            "total_loyalty_points": total_loyalty_points,
            "total_driver_invitations": total_driver_invitations,
            "pending_driver_invitations": pending_driver_invitations,
            "total_bulk_uploads": total_bulk_uploads,
            "total_processed_bulk_uploads": total_processed_bulk_uploads,
            "total_validated_addresses": total_validated_addresses,
            "payment_success_rate": payment_success_rate,
            "average_driver_rating": round(average_driver_rating, 2),
            "top_service_type_name": top_service_type_name,
            "top_service_type_usage": top_service_type_usage,
            "total_failed_bookings": total_failed_bookings,
            "total_cancelled_bookings": total_cancelled_bookings,
            "chart_data": chart_data,
            "revenue_chart_data": revenue_chart_data,
            "payout_chart_data": payout_chart_data,
            "status_chart_data": status_chart_data,
            "role_chart_data": role_chart_data,
            "service_chart_data": service_chart_data,
            "ratings_chart_data": ratings_chart_data,
            "payment_method_chart_data": payment_method_chart_data,
            "recent_bookings_table": recent_bookings_table,
            # Route/Hub/Driver KPIs
            "total_routes": total_routes,
            "route_status_counts": route_status_counts,
            "total_hubs": total_hubs,
            "total_active_bookings": total_active_bookings,
            "total_completed_bookings": total_completed_bookings,
            "hub_metrics": list(hub_metrics),  # For table display
            "hub_chart_data": hub_chart_data,  # For bar chart
            "pending_routes_with_suggestions": pending_routes_with_suggestions,  # Table for assignment help
            "total_assigned_jobs": total_assigned_jobs,
            "drivers_with_jobs": drivers_with_jobs,
            "recent_routes_table": recent_routes_table,
            "route_chart_data": json.dumps(
                route_chart_data
            ),  # JSON for JS rendering if needed
        }
    )
    return context

    return context
