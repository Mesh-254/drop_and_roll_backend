from celery import shared_task
from django.core.mail import send_mail
from django.conf import settings
from .models import Ticket

@shared_task
def send_ticket_notification(ticket_id, event_type):
    ticket = Ticket.objects.get(id=ticket_id)
    recipient = ticket.user.email if ticket.user else ticket.guest_email
    subject = f"Ticket Update: {ticket.subject}"
    message = f"Your ticket (ID: {ticket.id}) has been {event_type}. Status: {ticket.status}."
    send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [recipient])