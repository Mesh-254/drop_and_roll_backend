from rest_framework import viewsets, mixins, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db import transaction

from drf_yasg.utils import swagger_auto_schema


from .models import (
    TrackingSession,
    TrackingEvent,
    DriverLocation,
    Geofence,
    ProofOfDelivery,
    WebhookSubscription,
    TrackingStatus,
)
from bookings.models import Booking, BookingStatus
from .serializers import (
    TrackingSessionSerializer,
    TrackingSessionCreateSerializer,
    TrackingEventSerializer,
    DriverLocationSerializer,
    DriverLocationCreateSerializer,
    GeofenceSerializer,
    ProofOfDeliverySerializer,
    WebhookSubscriptionSerializer,
)
from .permissions import IsAdmin, IsDriver, IsCustomer


class TrackingSessionViewSet(viewsets.ModelViewSet):
    queryset = TrackingSession.objects.select_related("booking")
    serializer_class = TrackingSessionSerializer

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return TrackingSession.objects.none()
        u = self.request.user
        qs = super().get_queryset()
        if not u.is_authenticated:
            return qs.none()
        role = getattr(u, "role", None)
        if role == "customer":
            return qs.filter(booking__customer=u)
        if role == "driver":
            return qs.filter(booking__driver__user=u)
        return qs

    def get_permissions(self):
        if self.action in ["create", "update", "partial_update", "destroy"]:
            return [IsAdmin()]
        return super().get_permissions()

    def get_serializer_class(self):
        if self.action == "create":
            return TrackingSessionCreateSerializer
        return TrackingSessionSerializer

    @swagger_auto_schema(method="get", responses={200: TrackingSessionSerializer})
    @action(methods=["get"], detail=False, url_path="public/(?P<token>[0-9a-fA-F-]{36})")
    def public_lookup(self, request, token=None):
        session = get_object_or_404(
            TrackingSession, public_token=token, public_enabled=True)
        return Response(TrackingSessionSerializer(session).data)

    @swagger_auto_schema(
        method="post",
        request_body=TrackingEventSerializer,
        responses={201: TrackingEventSerializer}
    )
    @action(methods=["post"], detail=True, url_path="events")
    def add_event(self, request, pk=None):
        session = self.get_object()
        s = TrackingEventSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        ev = s.save(session=session)
        session.last_event_at = ev.created_at
        if ev.code == "out_for_delivery":
            session.status = TrackingStatus.EN_ROUTE
        elif ev.code == "arrived":
            session.status = TrackingStatus.NEARBY
        elif ev.code == "delivered":
            session.status = TrackingStatus.DELIVERED
            session.ended_at = timezone.now()
        elif ev.code == "failed":
            session.status = TrackingStatus.FAILED
            session.ended_at = timezone.now()
        session.save(update_fields=["status", "last_event_at", "ended_at"])
        return Response(TrackingEventSerializer(ev).data, status=201)


class DriverLocationViewSet(mixins.CreateModelMixin,
                            mixins.ListModelMixin,
                            viewsets.GenericViewSet):
    serializer_class = DriverLocationSerializer

    def get_permissions(self):
        if self.action in ["create"]:
            return [IsDriver()]
        if self.action in ["list"]:
            return [IsAdmin()]
        return super().get_permissions()

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return DriverLocation.objects.none()
        # Admin can filter by session via ?session=<uuid>
        qs = DriverLocation.objects.all()
        session_id = self.request.query_params.get("session")
        if session_id:
            qs = qs.filter(session_id=session_id)
        return qs

    def create(self, request, *args, **kwargs):
        session_id = request.query_params.get("session")
        if not session_id:
            return Response({"detail": "session query param required"}, status=400)
        session = get_object_or_404(TrackingSession, pk=session_id)
        s = DriverLocationCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        loc = DriverLocation.objects.create(
            session=session,
            driver=request.user,
            **s.validated_data,
        )
        return Response(DriverLocationSerializer(loc).data, status=201)


class ProofOfDeliveryViewSet(mixins.CreateModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = ProofOfDelivery.objects.select_related("booking")
    serializer_class = ProofOfDeliverySerializer
    parser_classes = [MultiPartParser, FormParser]

    def get_permissions(self):
        if self.action in ["create", "get_by_booking"]:
            return [IsDriver()]
        if self.action in ["retrieve"]:
            return [IsAdmin()]
        return super().get_permissions()

    def create(self, request, *args, **kwargs):
        booking_id = request.query_params.get("booking")
        if not booking_id:
            return Response({"detail": "booking query param required"}, status=400)
        booking = get_object_or_404(Booking, pk=booking_id)
        s = ProofOfDeliverySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        with transaction.atomic():
            pod = s.save(booking=booking)
            # Auto-update booking and tracking session status
            if booking.status != BookingStatus.DELIVERED:
                booking.status = BookingStatus.DELIVERED
                booking.updated_at = timezone.now()
                booking.save(update_fields=["status", "updated_at"])
                # Update tracking session if exists
                tracking_session = getattr(booking, "tracking_session", None)
                if tracking_session and tracking_session.status != TrackingStatus.DELIVERED:
                    tracking_session.status = TrackingStatus.DELIVERED
                    tracking_session.ended_at = timezone.now()
                    tracking_session.save(update_fields=["status", "ended_at"])
                    TrackingEvent.objects.create(
                        session=tracking_session,
                        code="delivered",
                        message="Proof of delivery submitted",
                    )
        return Response(ProofOfDeliverySerializer(pod).data, status=201)

    @swagger_auto_schema(
        method="get",
        responses={200: ProofOfDeliverySerializer, 404: {
            "detail": "Proof of delivery not found"}}
    )
    @action(methods=["get"], detail=False, url_path="by-booking")
    def get_by_booking(self, request):
        """
        Fetch Proof of Delivery by booking ID.
        Example: GET /api/tracking/pod/by-booking/?booking=<booking_id>
        """
        booking_id = request.query_params.get("booking")
        if not booking_id:
            return Response({"detail": "booking query param required"}, status=400)

        pod = get_object_or_404(ProofOfDelivery, booking_id=booking_id)
        serializer = ProofOfDeliverySerializer(pod)
        return Response(serializer.data, status=200)


class GeofenceViewSet(viewsets.ModelViewSet):
    queryset = Geofence.objects.all()
    serializer_class = GeofenceSerializer

    def get_permissions(self):
        if self.action in ["list", "retrieve"]:
            return [IsAdmin()]
        return [IsAdmin()]


class WebhookSubscriptionViewSet(viewsets.ModelViewSet):
    serializer_class = WebhookSubscriptionSerializer

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return WebhookSubscription.objects.none()
        return WebhookSubscription.objects.filter(customer=self.request.user)

    def get_permissions(self):
        return [IsCustomer()]
