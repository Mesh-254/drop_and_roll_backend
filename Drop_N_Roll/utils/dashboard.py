import json
from datetime import timedelta
from django.db.models import Sum, Count, Avg, Q
from django.utils import timezone
from django.contrib.auth import get_user_model
from bookings.models import Booking, Quote, ServiceType, ShippingType, RecurringSchedule, BulkUpload, Address, Route, Hub, BookingStatus
from driver.models import DriverProfile, DriverPayout, DriverRating, DriverInvitation
from payments.models import PaymentTransaction, Refund, Wallet
from support.models import Ticket, TicketStatus

User = get_user_model()


def dashboard_callback(request, context):
    # Restrict sensitive data to superusers
    if not request.user.is_superuser:
        context.update({
            'error': 'Access restricted to superusers.'
        })
        return context

    # KPI Queries
    total_users = User.objects.count()
    users_by_role = User.objects.values('role').annotate(
        count=Count('id')).order_by('role')
    total_customers = next(
        (item['count'] for item in users_by_role if item['role'] == 'customer'), 0)
    total_drivers = next(
        (item['count'] for item in users_by_role if item['role'] == 'driver'), 0)
    total_admins = next(
        (item['count'] for item in users_by_role if item['role'] == 'admin'), 0)

    total_bookings = Booking.objects.count()
    bookings_by_status = Booking.objects.values(
        'status').annotate(count=Count('id')).order_by('status')
    booking_status_counts = {item['status']: item['count']
                             for item in bookings_by_status}

    total_quotes = Quote.objects.count()
    total_service_types = ServiceType.objects.count()
    total_shipping_types = ShippingType.objects.count()
    total_revenue = Booking.objects.aggregate(
        total=Sum('final_price'))['total'] or 0
    average_booking_value = Booking.objects.aggregate(
        avg=Avg('final_price'))['avg'] or 0
    total_recurring_schedules = RecurringSchedule.objects.filter(
        active=True).count()

    drivers_by_status = DriverProfile.objects.values(
        'status').annotate(count=Count('id')).order_by('status')
    driver_status_counts = {item['status']: item['count']
                            for item in drivers_by_status}

    total_payouts = DriverPayout.objects.aggregate(total=Sum('amount'))[
        'total'] or 0
    total_refunds = Refund.objects.aggregate(total=Sum('amount'))['total'] or 0
    total_wallet_balance = Wallet.objects.aggregate(
        total=Sum('balance'))['total'] or 0
    total_loyalty_points = User.objects.filter(role='customer').aggregate(
        total=Sum('loyalty_points'))['total'] or 0
    total_driver_invitations = DriverInvitation.objects.count()
    pending_driver_invitations = DriverInvitation.objects.filter(
        status=DriverInvitation.Status.PENDING).count()
    total_bulk_uploads = BulkUpload.objects.count()
    total_processed_bulk_uploads = BulkUpload.objects.filter(
        processed=True).count()
    total_validated_addresses = Address.objects.filter(validated=True).count()

    total_successful_payments = PaymentTransaction.objects.filter(
        status='success').count()
    total_payments = PaymentTransaction.objects.count()
    payment_success_rate = (total_successful_payments /
                            total_payments * 100) if total_payments > 0 else 0
    average_driver_rating = DriverRating.objects.aggregate(avg=Avg('rating'))[
        'avg'] or 0

    top_service_type = ServiceType.objects.annotate(
        usage=Count('quotes__bookings')).order_by('-usage').first()
    top_service_type_name = top_service_type.name if top_service_type else 'N/A'
    top_service_type_usage = top_service_type.usage if top_service_type else 0
    total_failed_bookings = Booking.objects.filter(status='failed').count()
    total_cancelled_bookings = Booking.objects.filter(
        status='cancelled').count()

    # Support KPIs
    total_tickets = Ticket.objects.count()
    open_tickets = Ticket.objects.filter(
        status=TicketStatus.OPEN).count()  # Assuming OPEN in TicketStatus
    # Adjust 'type' filter as per your TicketType
    complaints = Ticket.objects.filter(type='complaint').count()
    # average_resolution_time = Ticket.objects.filter(status=TicketStatus.RESOLVED).aggregate(
    #     # Example metric
    #     avg=Avg(updated_at - created_at))['avg'] or timedelta(0)

    # Recent Open Tickets Table (similar to recent_bookings)
    recent_open_tickets = Ticket.objects.filter(status=TicketStatus.OPEN).order_by('-created_at')[:10].values(
        'id', 'subject', 'status', 'priority', 'created_at', 'user__email', 'guest_email'
    )
    recent_open_tickets_table = [
        {
            'id': str(ticket['id'])[:8],
            'subject': ticket['subject'],
            'user': ticket['user__email'] or ticket['guest_email'],
            'priority': ticket['priority'],
            'status': ticket['status'],
            'created_at': ticket['created_at'].strftime('%Y-%m-%d %H:%M')
        }
        for ticket in recent_open_tickets]

    # ********************************************************************************
    # Add Route/Hub/Driver KPIs
    # ********************************************************************************

    total_routes = Route.objects.count()
    routes_by_status = Route.objects.values(
        'status').annotate(count=Count('id'))
    route_status_counts = {item['status']: item['count']
                           for item in routes_by_status}
    # NEW: Hub KPIs (total hubs, total active/completed across hubs)
    total_hubs = Hub.objects.count()
    total_active_bookings = Booking.objects.filter(
        status__in=[BookingStatus.SCHEDULED, BookingStatus.ASSIGNED, 
                    BookingStatus.PICKED_UP, BookingStatus.AT_HUB, BookingStatus.IN_TRANSIT]
    ).count()
    total_completed_bookings = Booking.objects.filter(status=BookingStatus.DELIVERED).count()

    # NEW: Enhanced hub metrics table data (with routes and drivers)
    hub_metrics = Hub.objects.annotate(
        active_bookings=Count('hub_bookings', filter=Q(hub_bookings__status__in=[
            BookingStatus.SCHEDULED, BookingStatus.ASSIGNED, 
            BookingStatus.PICKED_UP, BookingStatus.AT_HUB, BookingStatus.IN_TRANSIT
        ])),
        completed_bookings=Count('hub_bookings', filter=Q(hub_bookings__status=BookingStatus.DELIVERED)),
        routes_count=Count('routes'),
        active_drivers=Count('drivers', filter=Q(drivers__status='active'))
    ).values('name', 'active_bookings', 'completed_bookings', 'routes_count', 'active_drivers')

    # NEW: Hub chart (bar chart for active/completed per hub)
    hub_labels = [item['name'] for item in hub_metrics]
    active_data = [item['active_bookings'] for item in hub_metrics]
    completed_data = [item['completed_bookings'] for item in hub_metrics]
    hub_chart_data = json.dumps({
        'labels': hub_labels,
        'datasets': [
            {'label': 'Active Bookings', 'data': active_data, 'backgroundColor': '#3b82f6'},
            {'label': 'Completed Bookings', 'data': completed_data, 'backgroundColor': '#10b981'},
        ]
    })

    # NEW: Pending routes overview (to help assignment) - List pending routes with hub and suggested drivers
    pending_routes = Route.objects.filter(status='pending').select_related('hub').annotate(
        bookings_count=Count('bookings')
    ).values('id', 'hub__name', 'leg_type', 'total_time_hours', 'bookings_count')
    
    pending_routes_with_suggestions = []
    for pr in pending_routes:
        if pr['hub__name']:
            # Suggest top 3 available drivers from same hub (similar to API logic)
            suggested_drivers = DriverProfile.objects.filter(
                hub__name=pr['hub__name'],
                status='active'
            ).select_related('user')[:3].values('user__full_name')
            suggested_drivers = list(suggested_drivers)
        pr['suggested_drivers'] = suggested_drivers
        pending_routes_with_suggestions.append(pr)

    total_assigned_jobs = Booking.objects.filter(status='assigned').count()
    drivers_with_jobs = DriverProfile.objects.annotate(job_count=Count(
        'bookings')).filter(job_count__gt=0).count()  # FIXED HERE

    # Recent Routes Table (optimized with prefetch)
    recent_routes = Route.objects.prefetch_related(
        'bookings', 'driver').order_by('-id')[:5]
    recent_routes_table = [
        {
            'id': route.id,
            'driver': route.driver.user.full_name if route.driver else 'Unassigned',
            'leg_type': route.leg_type,
            'status': route.status,
            'bookings': route.bookings.count(),
            'total_time': f"{route.total_time_hours:.2f} hours" if route.total_time_hours else "N/A",
        } for route in recent_routes
    ]

    # Charts: Route status pie (handle empty data)
    route_chart_data = {
        'labels': list(route_status_counts.keys()) or ['No Data'],
        'data': list(route_status_counts.values()) or [0],
        # Colors (cycle if more)
        'backgroundColor': ['#FF6384', '#36A2EB', '#FFCE56'],
    }

    # Chart Data: Bookings over the last 7 days
    today = timezone.now().date()
    bookings_data = []
    revenue_data = []
    payout_data = []
    for i in range(7):
        day = today - timedelta(days=i)
        count = Booking.objects.filter(created_at__date=day).count()
        revenue = Booking.objects.filter(created_at__date=day).aggregate(
            total=Sum('final_price'))['total'] or 0
        payouts = DriverPayout.objects.filter(
            created_at__date=day).aggregate(total=Sum('amount'))['total'] or 0
        bookings_data.append(
            {'date': day.strftime('%Y-%m-%d'), 'count': count})
        revenue_data.append(
            {'date': day.strftime('%Y-%m-%d'), 'revenue': revenue})
        payout_data.append(
            {'date': day.strftime('%Y-%m-%d'), 'payout': payouts})

    chart_data = json.dumps({
        'labels': [d['date'] for d in reversed(bookings_data)],
        'datasets': [{
            'label': 'Bookings',
            'data': [d['count'] for d in reversed(bookings_data)],
            'borderColor': 'rgb(168, 85, 247)',
            'backgroundColor': 'rgba(168, 85, 247, 0.2)',
            'fill': True,
        }]
    })

    revenue_chart_data = json.dumps({
        'labels': [d['date'] for d in reversed(revenue_data)],
        'datasets': [{
            'label': 'Revenue',
            'data': [int(d['revenue']) for d in reversed(revenue_data)],
            'borderColor': 'rgb(34, 197, 94)',
            'backgroundColor': 'rgba(34, 197, 94, 0.2)',
            'fill': True,
        }]
    })

    payout_chart_data = json.dumps({
        'labels': [d['date'] for d in reversed(payout_data)],
        'datasets': [{
            'label': 'Payouts',
            'data': [d['payout'] for d in reversed(payout_data)],
            'borderColor': 'rgb(59, 130, 246)',
            'backgroundColor': 'rgba(59, 130, 246, 0.2)',
            'fill': True,
        }]
    })

    # Chart: Bookings by Status
    status_labels = list(booking_status_counts.keys())
    status_data = list(booking_status_counts.values())
    status_chart_data = json.dumps({
        'labels': status_labels,
        'datasets': [{
            'data': status_data,
            'backgroundColor': ['#999', '#0ea5e9', '#f59e0b', '#6366f1', '#06b6d4', '#16a34a', '#ef4444', '#b91c1c'],
        }]
    })

    # Chart: Users by Role
    role_labels = [item['role'] for item in users_by_role]
    role_data = [item['count'] for item in users_by_role]
    role_chart_data = json.dumps({
        'labels': role_labels,
        'datasets': [{
            'data': role_data,
            'backgroundColor': ['#f59e0b', '#0ea5e9', '#16a34a'],
        }]
    })

    # Chart: Bookings by Service Type
    service_type_usage = ServiceType.objects.annotate(usage=Count(
        'quotes__bookings')).values('name', 'usage').order_by('-usage')
    service_labels = [item['name'] for item in service_type_usage]
    service_data = [item['usage'] for item in service_type_usage]
    service_chart_data = json.dumps({
        'labels': service_labels,
        'datasets': [{
            'label': 'Bookings',
            'data': service_data,
            'backgroundColor': 'rgb(168, 85, 247)',
        }]
    })

    # Chart: Ratings Distribution
    ratings_distribution = DriverRating.objects.values(
        'rating').annotate(count=Count('id')).order_by('rating')
    ratings_labels = [str(item['rating']) for item in ratings_distribution]
    ratings_data = [item['count'] for item in ratings_distribution]
    ratings_chart_data = json.dumps({
        'labels': ratings_labels,
        'datasets': [{
            'label': 'Ratings',
            'data': ratings_data,
            'backgroundColor': 'rgb(234, 179, 8)',
        }]
    })

    # Chart: Payments by Method
    payment_methods = PaymentTransaction.objects.values(
        'method__method_type').annotate(count=Count('id')).order_by('method__method_type')
    payment_method_labels = [item['method__method_type']
                             for item in payment_methods]
    payment_method_data = [item['count'] for item in payment_methods]
    payment_method_chart_data = json.dumps({
        'labels': payment_method_labels,
        'datasets': [{
            'data': payment_method_data,
            'backgroundColor': ['#ef4444', '#3b82f6', '#10b981', '#f59e0b', '#6b7280', '#6366f1'],
        }]
    })

    # Recent Bookings Table
    recent_bookings = Booking.objects.select_related('customer', 'quote', 'quote__service_type').order_by('-created_at')[:10].values(
        'id', 'status', 'final_price', 'created_at', 'customer__full_name', 'quote__service_type__name'
    )
    recent_bookings_table = [
        {
            'id': str(booking['id'])[:8],
            'customer': booking['customer__full_name'] or 'Guest',
            'service_type': booking['quote__service_type__name'] or 'Unknown',
            'status': booking['status'],
            'final_price': f"GBP {booking['final_price']}",
            'created_at': booking['created_at'].strftime('%Y-%m-%d %H:%M')
        }
        for booking in recent_bookings
    ]

    # Update context with KPIs and data
    context.update({
        'total_users': total_users,
        'total_customers': total_customers,
        'total_drivers': total_drivers,
        'total_admins': total_admins,
        'total_bookings': total_bookings,
        'booking_status_counts': booking_status_counts,
        'total_quotes': total_quotes,
        'total_service_types': total_service_types,
        'total_shipping_types': total_shipping_types,
        'total_revenue': total_revenue,
        'average_booking_value': average_booking_value,
        'total_recurring_schedules': total_recurring_schedules,
        'driver_status_counts': driver_status_counts,
        'total_payouts': total_payouts,
        'total_refunds': total_refunds,
        'total_wallet_balance': total_wallet_balance,
        'total_loyalty_points': total_loyalty_points,
        'total_driver_invitations': total_driver_invitations,
        'pending_driver_invitations': pending_driver_invitations,
        'total_bulk_uploads': total_bulk_uploads,
        'total_processed_bulk_uploads': total_processed_bulk_uploads,
        'total_validated_addresses': total_validated_addresses,
        'payment_success_rate': round(payment_success_rate, 2),
        'average_driver_rating': round(average_driver_rating, 2),
        'top_service_type_name': top_service_type_name,
        'top_service_type_usage': top_service_type_usage,
        'total_failed_bookings': total_failed_bookings,
        'total_cancelled_bookings': total_cancelled_bookings,
        'chart_data': chart_data,
        'revenue_chart_data': revenue_chart_data,
        'payout_chart_data': payout_chart_data,
        'status_chart_data': status_chart_data,
        'role_chart_data': role_chart_data,
        'service_chart_data': service_chart_data,
        'ratings_chart_data': ratings_chart_data,
        'payment_method_chart_data': payment_method_chart_data,
        'recent_bookings_table': recent_bookings_table,
        # Route/Hub/Driver KPIs
        'total_routes': total_routes,
        'route_status_counts': route_status_counts,

        'total_hubs': total_hubs,
        'total_active_bookings': total_active_bookings,
        'total_completed_bookings': total_completed_bookings,
        'hub_metrics': list(hub_metrics),  # For table display
        'hub_chart_data': hub_chart_data,  # For bar chart
        'pending_routes_with_suggestions': pending_routes_with_suggestions,  # Table for assignment help

        'total_assigned_jobs': total_assigned_jobs,
        'drivers_with_jobs': drivers_with_jobs,
        'recent_routes_table': recent_routes_table,
        'route_chart_data': json.dumps(route_chart_data),  # JSON for JS rendering if needed
    })
    return context
