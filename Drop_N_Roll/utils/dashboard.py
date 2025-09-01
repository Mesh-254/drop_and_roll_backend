import json
from datetime import timedelta
from django.db.models import Sum, Count
from django.utils import timezone
from django.contrib.auth import get_user_model
from bookings.models import Booking, Quote, ServiceType, ShippingType, RecurringSchedule
from driver.models import DriverProfile

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
    users_by_role = User.objects.values('role').annotate(count=Count('id')).order_by('role')
    total_customers = next((item['count'] for item in users_by_role if item['role'] == 'customer'), 0)
    total_drivers = next((item['count'] for item in users_by_role if item['role'] == 'driver'), 0)
    total_admins = next((item['count'] for item in users_by_role if item['role'] == 'admin'), 0)

    total_bookings = Booking.objects.count()
    bookings_by_status = Booking.objects.values('status').annotate(count=Count('id')).order_by('status')
    booking_status_counts = {item['status']: item['count'] for item in bookings_by_status}

    total_quotes = Quote.objects.count()
    total_service_types = ServiceType.objects.count()
    total_shipping_types = ShippingType.objects.count()
    total_revenue = Booking.objects.aggregate(total=Sum('final_price'))['total'] or 0
    total_recurring_schedules = RecurringSchedule.objects.filter(active=True).count()

    drivers_by_status = DriverProfile.objects.values('status').annotate(count=Count('id')).order_by('status')
    driver_status_counts = {item['status']: item['count'] for item in drivers_by_status}

    # Chart Data: Bookings over the last 7 days
    today = timezone.now().date()
    bookings_data = []
    for i in range(7):
        day = today - timedelta(days=i)
        count = Booking.objects.filter(created_at__date=day).count()
        bookings_data.append({'date': day.strftime('%Y-%m-%d'), 'count': count})
    chart_data = json.dumps({
        'labels': [d['date'] for d in reversed(bookings_data)],
        'datasets': [{
            'label': 'Bookings',
            'data': [d['count'] for d in reversed(bookings_data)],
            'borderColor': 'rgb(168, 85, 247)',  # From UNFOLD["COLORS"]["primary"]
            'backgroundColor': 'rgba(168, 85, 247, 0.2)',
            'fill': True,
        }]
    })

    # Recent Bookings Table
    recent_bookings = Booking.objects.select_related('customer', 'quote', 'quote__service_type').order_by('-created_at')[:10].values(
        'id', 'status', 'final_price', 'created_at', 'customer__full_name', 'quote__service_type__name'
    )
    recent_bookings_table = [
        {
            'id': str(booking['id'])[:8],  # Shortened UUID for display
            'customer': booking['customer__full_name'] or 'Guest',
            'service_type': booking['quote__service_type__name'] or 'Unknown',
            'status': booking['status'],
            'final_price': f"KES {booking['final_price']}",
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
        'total_recurring_schedules': total_recurring_schedules,
        'driver_status_counts': driver_status_counts,
        'chart_data': chart_data,
        'recent_bookings_table': recent_bookings_table,
    })
    return context