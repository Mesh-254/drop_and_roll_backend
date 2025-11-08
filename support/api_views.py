from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from django.db.transaction import atomic
from django.shortcuts import get_object_or_404
import logging

from .models import Ticket, TicketStatus
from .serializers import TicketSerializer, TicketCommentSerializer
from .permissions import IsSupportOrAdmin, IsOwnerOrSupport  # Define these below
from .tasks import send_ticket_notification  # We'll add this in Step 6
from users.permissions import IsAdmin  # From your users app

logger = logging.getLogger(__name__)


class TicketViewSet(viewsets.ModelViewSet):
    serializer_class = TicketSerializer
    permission_classes = [AllowAny]  # Overridden per action

    def get_permissions(self):
        if self.action in ["list", "retrieve"]:
            return [IsOwnerOrSupport()]
        if self.action in ["create"]:
            return [AllowAny()]  # Guests can create
        if self.action in ["update", "partial_update", "destroy", "add_comment", "update_status"]:
            return [IsSupportOrAdmin()]
        return super().get_permissions()

    def get_queryset(self):
        qs = Ticket.objects.select_related(
            "user", "booking", "created_by", "assigned_to").prefetch_related("comments", "attachments")
        user = self.request.user
        guest_email = self.request.query_params.get("guest_email", "").lower()
        status_filter = self.request.query_params.get("status")
        booking_id = self.request.query_params.get("booking_id")

        if user.is_authenticated:
            if user.is_admin or user.role == "support":  # Assume 'support' role; extend User.Role if needed
                pass  # All tickets
            else:
                qs = qs.filter(user=user)
        elif guest_email:
            qs = qs.filter(guest_email=guest_email, user__isnull=True)
        else:
            qs = qs.none()  # No access

        if status_filter:
            qs = qs.filter(status=status_filter)
        if booking_id:
            qs = qs.filter(booking_id=booking_id)
        return qs.order_by("-created_at")  # Efficient ordering

    @atomic
    def perform_create(self, serializer):
        ticket = serializer.save()
        send_ticket_notification.delay(ticket.id, "created")  # Async email
        logger.info(
            f"Ticket {ticket.id} created by {self.request.user or ticket.guest_email}")

    @action(methods=["post"], detail=True, url_path="add-comment")
    def add_comment(self, request, pk=None):
        ticket = get_object_or_404(Ticket, pk=pk)
        serializer = TicketCommentSerializer(
            data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        comment = serializer.save(ticket=ticket, user=request.user)
        send_ticket_notification.delay(ticket.id, "updated")  # Notify
        return Response(TicketCommentSerializer(comment).data, status=status.HTTP_201_CREATED)

    @action(methods=["patch"], detail=True, url_path="update-status")
    def update_status(self, request, pk=None):
        ticket = get_object_or_404(Ticket, pk=pk)
        new_status = request.data.get("status")
        if new_status not in dict(TicketStatus.choices):
            return Response({"error": "Invalid status"}, status=status.HTTP_400_BAD_REQUEST)
        ticket.status = new_status
        ticket.save()
        send_ticket_notification.delay(ticket.id, "status_changed")
        return Response({"detail": "Status updated"})
