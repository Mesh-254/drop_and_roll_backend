import json
from datetime import timedelta, datetime
from django.db.models import Sum, Count, Avg, Q, F
from django.utils import timezone
from django.contrib.auth import get_user_model
from bookings.models import Booking, Quote, ServiceType, ShippingType, RecurringSchedule, BulkUpload, Address
from driver.models import DriverProfile, DriverPayout, DriverRating, DriverInvitation
from payments.models import PaymentTransaction, Refund, Wallet

User = get_user_model()


def dashboard_callback(request, context):
    # Restrict to superusers
    if not request.user.is_superuser:
        context.update({'error': 'Access restricted to superusers.'})
        return context

    # ==================== DATE RANGE FILTERING ====================
    today = timezone.now().date()

    from_date_str = request.GET.get('from_date')
    to_date_str = request.GET.get('to_date')

    from_date = None
    to_date = None
    date_error = None

    if from_date_str:
        try:
            from_date = datetime.strptime(from_date_str, '%Y-%m-%d').date()
        except ValueError:
            date_error = 'Invalid "from" date. Use YYYY-MM-DD.'

    if to_date_str:
        try:
            to_date = datetime.strptime(to_date_str, '%Y-%m-%d').date()
        except ValueError:
            date_error = 'Invalid "to" date. Use YYYY-MM-DD.'

    if not date_error and from_date and to_date and from_date > to_date:
        date_error = '"From" date cannot be after "To" date.'

    has_date_range = bool(not date_error and (from_date or to_date))

    # Build Q filter
    booking_filter = Q()
    if from_date:
        booking_filter &= Q(created_at__date__gte=from_date)
    if to_date:
        booking_filter &= Q(created_at__date__lte=to_date)

    # Apply filter
    bookings_qs = Booking.objects.filter(booking_filter) if has_date_range else Booking.objects.all()
    quotes_qs = Quote.objects.filter(booking_filter) if has_date_range else Quote.objects.all()
    payout_filter = booking_filter
    payout_qs = DriverPayout.objects.filter(payout_filter) if has_date_range else DriverPayout.objects.all()

    # ==================== KPIs (Filtered) ====================
    total_users = User.objects.count()
    users_by_role = User.objects.values('role').annotate(count=Count('id'))
    total_customers = next((i['count'] for i in users_by_role if i['role'] == 'customer'), 0)
    total_drivers = next((i['count'] for i in users_by_role if i['role'] == 'driver'), 0)
    total_admins = next((i['count'] for i in users_by_role if i['role'] == 'admin'), 0)

    total_bookings = bookings_qs.count()
    bookings_by_status = bookings_qs.values('status').annotate(count=Count('id'))
    booking_status_counts = {item['status']: item['count'] for item in bookings_by_status}

    total_quotes = quotes_qs.count()
    total_service_types = ServiceType.objects.count()
    total_shipping_types = ShippingType.objects.count()

    total_revenue = bookings_qs.aggregate(total=Sum('final_price'))['total'] or 0
    average_booking_value = bookings_qs.aggregate(avg=Avg('final_price'))['avg'] or 0

    total_recurring_schedules = RecurringSchedule.objects.filter(active=True).count()

    drivers_by_status = DriverProfile.objects.values('status').annotate(count=Count('id'))
    driver_status_counts = {item['status']: item['count'] for item in drivers_by_status}

    total_payouts = payout_qs.aggregate(total=Sum('amount'))['total'] or 0
    total_refunds = Refund.objects.aggregate(total=Sum('amount'))['total'] or 0
    total_wallet_balance = Wallet.objects.aggregate(total=Sum('balance'))['total'] or 0
    total_loyalty_points = User.objects.filter(role='customer').aggregate(total=Sum('loyalty_points'))['total'] or 0
    total_driver_invitations = DriverInvitation.objects.count()
    pending_driver_invitations = DriverInvitation.objects.filter(status=DriverInvitation.Status.PENDING).count()
    total_bulk_uploads = BulkUpload.objects.count()
    total_processed_bulk_uploads = BulkUpload.objects.filter(processed=True).count()
    total_validated_addresses = Address.objects.filter(validated=True).count()

    # Payments
    payment_qs = PaymentTransaction.objects.all()
    if has_date_range:
        payment_qs = payment_qs.filter(booking_filter)
    total_successful_payments = payment_qs.filter(status='success').count()
    total_payments = payment_qs.count()
    payment_success_rate = round(total_successful_payments / total_payments * 100, 2) if total_payments else 0

    average_driver_rating = DriverRating.objects.aggregate(avg=Avg('rating'))['avg'] or 0

    # ==================== TOP SERVICE TYPE (FIXED WITH F()) ====================
    if has_date_range:
        top = (
            bookings_qs
            .values(service_type_name=F('quote__service_type__name'))
            .annotate(usage=Count('id'))
            .order_by('-usage')
            .first()
        )
    else:
        top = (
            Booking.objects
            .values(service_type_name=F('quote__service_type__name'))
            .annotate(usage=Count('id'))
            .order_by('-usage')
            .first()
        )

    top_service_type_name = top['service_type_name'] if top else 'N/A'
    top_service_type_usage = top['usage'] if top else 0

    total_failed_bookings = bookings_qs.filter(status='failed').count()
    total_cancelled_bookings = bookings_qs.filter(status='cancelled').count()

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
        day_str = day.strftime('%Y-%m-%d')
        day_bookings = bookings_qs.filter(created_at__date=day).count()
        day_revenue = bookings_qs.filter(created_at__date=day).aggregate(t=Sum('final_price'))['t'] or 0
        day_payout = payout_qs.filter(created_at__date=day).aggregate(t=Sum('amount'))['t'] or 0

        bookings_daily.append({'date': day_str, 'count': day_bookings})
        revenue_daily.append({'date': day_str, 'revenue': float(day_revenue)})
        payout_daily.append({'date': day_str, 'payout': float(day_payout)})

    chart_data = json.dumps({
        'labels': [d['date'] for d in bookings_daily],
        'datasets': [{
            'label': 'Bookings',
            'data': [d['count'] for d in bookings_daily],
            'borderColor': 'rgb(168, 85, 247)',
            'backgroundColor': 'rgba(168, 85, 247, 0.2)',
            'fill': True,
        }]
    })

    revenue_chart_data = json.dumps({
        'labels': [d['date'] for d in revenue_daily],
        'datasets': [{
            'label': 'Revenue',
            'data': [d['revenue'] for d in revenue_daily],
            'borderColor': 'rgb(34, 197, 94)',
            'backgroundColor': 'rgba(34, 197, 94, 0.2)',
            'fill': True,
        }]
    })

    payout_chart_data = json.dumps({
        'labels': [d['date'] for d in payout_daily],
        'datasets': [{
            'label': 'Payouts',
            'data': [d['payout'] for d in payout_daily],
            'borderColor': 'rgb(59, 130, 246)',
            'backgroundColor': 'rgba(59, 130, 246, 0.2)',
            'fill': True,
        }]
    })

    # ==================== OTHER CHARTS ====================
    status_labels = list(booking_status_counts.keys())
    status_data = list(booking_status_counts.values())
    status_chart_data = json.dumps({
        'labels': status_labels,
        'datasets': [{'data': status_data, 'backgroundColor': ['#999', '#0ea5e9', '#f59e0b', '#6366f1', '#06b6d4', '#16a34a', '#ef4444', '#b91c1c']}]
    })

    role_labels = [item['role'] for item in users_by_role]
    role_data = [item['count'] for item in users_by_role]
    role_chart_data = json.dumps({
        'labels': role_labels,
        'datasets': [{'data': role_data, 'backgroundColor': ['#f59e0b', '#0ea5e9', '#16a34a']}]
    })

    service_type_usage = ServiceType.objects.annotate(usage=Count('quotes__bookings')).values('name', 'usage').order_by('-usage')
    service_labels = [item['name'] for item in service_type_usage]
    service_data = [item['usage'] for item in service_type_usage]
    service_chart_data = json.dumps({
        'labels': service_labels,
        'datasets': [{'label': 'Bookings', 'data': service_data, 'backgroundColor': 'rgb(168, 85, 247)'}]
    })

    ratings_distribution = DriverRating.objects.values('rating').annotate(count=Count('id')).order_by('rating')
    ratings_labels = [str(item['rating']) for item in ratings_distribution]
    ratings_data = [item['count'] for item in ratings_distribution]
    ratings_chart_data = json.dumps({
        'labels': ratings_labels,
        'datasets': [{'label': 'Ratings', 'data': ratings_data, 'backgroundColor': 'rgb(234, 179, 8)'}]
    })

    payment_methods = PaymentTransaction.objects.values('method__method_type').annotate(count=Count('id')).order_by('method__method_type')
    payment_method_labels = [item['method__method_type'] or 'Unknown' for item in payment_methods]
    payment_method_data = [item['count'] for item in payment_methods]
    payment_method_chart_data = json.dumps({
        'labels': payment_method_labels,
        'datasets': [{'data': payment_method_data, 'backgroundColor': ['#ef4444', '#3b82f6', '#10b981', '#f59e0b', '#6b7280', '#6366f1']}]
    })

    # ==================== RECENT BOOKINGS ====================
    recent_qs = bookings_qs.select_related('customer', 'quote', 'quote__service_type').order_by('-created_at')[:10]
    if not has_date_range:
        recent_qs = Booking.objects.select_related('customer', 'quote', 'quote__service_type').order_by('-created_at')[:10]

    recent_bookings = recent_qs.values(
        'id', 'status', 'final_price', 'created_at', 'customer__full_name', 'quote__service_type__name'
    )

    recent_bookings_table = [
        {
            'id': str(b['id'])[:8],
            'customer': b['customer__full_name'] or 'Guest',
            'service_type': b['quote__service_type__name'] or 'Unknown',
            'status': b['status'],
            'final_price': f"GBP {b['final_price']}",
            'created_at': b['created_at'].strftime('%Y-%m-%d %H:%M')
        }
        for b in recent_bookings
    ]

    # ==================== UPDATE CONTEXT ====================
    context.update({
        'date_from': request.GET.get('from_date', ''),
        'date_to': request.GET.get('to_date', ''),
        'date_error': date_error,
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
        'payment_success_rate': payment_success_rate,
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
    })

    return context