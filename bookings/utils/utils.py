# utils.py (add to existing)
from django.utils import timezone
from  ..models import Booking, BookingStatus

def format_datetime(dt):
    if not dt:
        return None
    return dt.strftime("%Y-%m-%d %I:%M %p")

def format_address(address):
    if not address:
        return None
    parts = [address.line1, address.city]
    if address.region:
        parts.append(address.region)
    if address.postal_code:
        parts.append(address.postal_code)
    return ", ".join(filter(None, parts))

def get_current_location(booking):
    status = booking.status
    if status == BookingStatus.PENDING:
        return "Awaiting payment confirmation"
    if status == BookingStatus.SCHEDULED:
        return "Scheduled for pickup"
    if status == BookingStatus.ASSIGNED:
        return "Driver assigned"
    if status == BookingStatus.PICKED_UP:
        return f"Picked up from {booking.pickup_address.city}"
    if status == BookingStatus.IN_TRANSIT:
        return "In transit"
    if status == BookingStatus.DELIVERED:
        return f"Delivered to {booking.dropoff_address.city}"
    if status in [BookingStatus.CANCELLED, BookingStatus.FAILED]:
        return "Delivery stopped"
    return "Status unknown"

def build_tracking_timeline(booking):
    steps = [
        {"status": "pending", "label": "Order Placed"},
        {"status": "scheduled", "label": "Scheduled"},
        {"status": "assigned", "label": "Driver Assigned"},
        {"status": "picked_up", "label": "Picked Up"},
        {"status": "in_transit", "label": "In Transit"},
        {"status": "delivered", "label": "Delivered"},
    ]

    current_idx = next((i for i, s in enumerate(steps) if s["status"] == booking.status), -1)
    
    timeline = []
    for i, step in enumerate(steps):
        is_completed = i < current_idx
        is_current = i == current_idx
        is_future = i > current_idx

        timestamp = None
        if is_completed:
            timestamp = format_datetime(booking.updated_at)  # or use audit log later
        elif is_current:
            timestamp = f"Active since {format_datetime(booking.updated_at)}"
        elif is_future and step["status"] == "delivered" and booking.scheduled_dropoff_at:
            timestamp = f"Est. {format_datetime(booking.scheduled_dropoff_at)}"

        timeline.append({
            "status": step["status"],
            "label": step["label"],
            "location": get_step_location(step["status"], booking),
            "timestamp": timestamp,
            "completed": is_completed,
            "current": is_current,
        })
    return timeline

def get_step_location(status, booking):
    mapping = {
        "pending": "Awaiting confirmation",
        "scheduled": "Preparing pickup",
        "assigned": "Driver en route to pickup",
        "picked_up": f"{booking.pickup_address.city}",
        "in_transit": "Between locations",
        "delivered": f"{booking.dropoff_address.city}",
    }
    return mapping.get(status, "Unknown")