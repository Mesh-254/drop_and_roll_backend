from decimal import Decimal

from drf_yasg import openapi  # type: ignore
from drf_yasg.utils import swagger_auto_schema  # type: ignore
from .tasks import send_booking_confirmation_email
from rest_framework import viewsets, status  # type: ignore
from rest_framework.decorators import action  # type: ignore
from rest_framework.permissions import IsAuthenticated, IsAuthenticatedOrReadOnly  # type: ignore
from rest_framework.response import Response  # type: ignore
from rest_framework.permissions import AllowAny  # type: ignore
from rest_framework.views import APIView  # type: ignore
from django.db.transaction import atomic  # type: ignore

from django.core.exceptions import ValidationError  # type: ignore
import uuid
import shortuuid  # type: ignore
from django.utils import timezone  # type: ignore
from payments.models import PaymentTransaction, PaymentStatus
from payments.serializers import PaymentTransactionSerializer

from .models import Quote, Booking, RecurringSchedule, BookingStatus, ShippingType, ServiceType
from .permissions import IsCustomer, IsAdminOrReadOnly, IsDriverOrAdmin
from .serializers import (
    QuoteRequestSerializer,
    QuoteSerializer,
    BookingCreateSerializer,
    BookingSerializer,
    RecurringScheduleSerializer, ShippingTypeSerializer, ServiceTypeSerializer,
)
from .utils.pricing import compute_quote

from django.db.models import Case, When, IntegerField


class QuoteViewSet(viewsets.GenericViewSet):
    queryset = Quote.objects.all()
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        method="post",
        request_body=QuoteRequestSerializer,
        responses={201: QuoteSerializer}
    )
    @action(methods=["post"], detail=False, url_path="compute", permission_classes=[AllowAny])
    @atomic
    def compute(self, request):
        serializer = QuoteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        shipping_type = ShippingType.objects.get(id=data["shipping_type_id"])
        service_type = ServiceType.objects.get(id=data["service_type_id"])

        base_price, final_price, breakdown = compute_quote(
            shipment_type=shipping_type.name,
            service_type=service_type.name,
            weight_kg=data["weight_kg"],
            distance_km=data["distance_km"],
            fragile=data["fragile"],
            insurance_amount=data["insurance_amount"],
            dimensions=data["dimensions"],
            surge=data["surge"],
            discount=data["discount"],
        )

        quote = Quote.objects.create(
            shipping_type=shipping_type,
            service_type=service_type,
            weight_kg=data["weight_kg"],
            distance_km=data["distance_km"],
            fragile=data["fragile"],
            insurance_amount=data["insurance_amount"],
            dimensions=data["dimensions"],
            base_price=base_price,
            surge_multiplier=data["surge"],
            discount_amount=data["discount"],
            final_price=final_price,
            meta=breakdown,
        )
        return Response(QuoteSerializer(quote).data, status=status.HTTP_201_CREATED)


class BookingViewSet(viewsets.ModelViewSet):
    queryset = Booking.objects.select_related("pickup_address", "dropoff_address", "customer", "driver", "quote").prefetch_related(
        "quote__shipping_type", "quote__service_type")
    serializer_class = BookingSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        guest_email = self.request.query_params.get("guest_email", "").lower()
        status_filter = self.request.query_params.get("status", "")  # From frontend (e.g., statusParam in JSX)

        # Apply user/role-based filters first
        if user.is_authenticated:
            role = getattr(user, "role", None)
            if role == "customer":
                qs = qs.filter(customer=user)
            elif role == "driver":
                qs = qs.filter(driver__user=user)
            elif role == "admin":
                pass  # All bookings for admins
            # Note: If role is none or other, qs remains unfiltered (consider adding else: qs = qs.none() for security)
        elif guest_email:
            # Allow guests to access their bookings (add customer__isnull=True for security, matching by_guest action)
            qs = qs.filter(guest_email=guest_email.lower(), customer__isnull=True)
        else:
            # Default: empty queryset for unauthenticated users without guest_email
            return qs.none()

        # Apply status filter if provided (after user filters)
        if status_filter:
            qs = qs.filter(status=status_filter)

        # Hybrid ordering: Annotate status priority (lower number = higher priority)
        qs = qs.annotate(
            status_priority=Case(
                When(status=BookingStatus.ASSIGNED, then=0),      # Highest: New assignments
                When(status=BookingStatus.PICKED_UP, then=1),     # Next: Ready to transit
                When(status=BookingStatus.IN_TRANSIT, then=2),    # Active: In progress
                When(status=BookingStatus.DELIVERED, then=3),     # Lower: Completed
                When(status=BookingStatus.SCHEDULED, then=4),     # Upcoming or pending
                default=5,                                        # Others (e.g., CANCELLED, FAILED) at bottom
                output_field=IntegerField()
            )
        ).order_by('status_priority', '-updated_at')  # Status priority asc, then most recent updates first

        return qs

    def get_object(self):
        # Use get_queryset to ensure proper filtering
        queryset = self.get_queryset()
        obj = super().get_object()  # This will raise 404 if no object is found
        return obj

    def get_permissions(self):
        if self.action in ["create", "by_guest", "bulk_upload", "recurring_list", "recurring_create", "retrieve"]:
            return [AllowAny()]
        if self.action in ["update", "partial_update", "destroy", "assign_driver"]:
            return [IsAdminOrReadOnly()]
        if self.action in ["set_status", "proof_of_delivery", "statuses"]:
            return [IsAuthenticated(), IsDriverOrAdmin()]
        return [IsAuthenticated()]

    def get_serializer_class(self):
        if self.action == "create":
            return BookingCreateSerializer
        return BookingSerializer

    @atomic
    def perform_create(self, serializer):
        user = self.request.user
        guest_email = serializer.validated_data.get("guest_email")

        # Check for pending bookings to enforce anti-spam limit
        pending_count = 0
        if user.is_authenticated:
            pending_count = Booking.objects.filter(
                customer=user, status=BookingStatus.PENDING
            ).count()
        elif guest_email:
            pending_count = Booking.objects.filter(
                guest_email=guest_email, customer__isnull=True, status=BookingStatus.PENDING
            ).count()

        # if pending_count >= 5:  # Anti-spam limit
        #     raise ValidationError("Too many pending bookings.")

        # Save the booking using serializer's logic
        booking = serializer.save(status=BookingStatus.PENDING,
                                  payment_expires_at=timezone.now() + timezone.timedelta(days=1))

        # Handle free bookings (final_price <= 0)
        if booking.final_price <= 0:
            booking.status = BookingStatus.SCHEDULED
            booking.tracking_number = f"BK-{shortuuid.uuid()[:6].upper()}"
            booking.save()
            send_booking_confirmation_email.delay(booking.id)
            return

        # Create payment transaction for non-free bookings
        user = booking.customer  # None for guests
        guest_email = booking.guest_email.lower() if not user else None
        tx = PaymentTransaction.objects.create(
            user=user,
            guest_email=guest_email,
            booking=booking,
            amount=booking.final_price,
            status=PaymentStatus.PENDING,
            reference=str(uuid.uuid4())[:12].replace("-", "")
        )
       
        # send_reminder.delay(booking.id, tx.reference, is_initial=True)
        self.tx_data = PaymentTransactionSerializer(tx).data

    def create(self, request, *args, **kwargs):
        super().create(request, *args, **kwargs)
        # Returns tx for redirect
        return Response(self.tx_data, status=status.HTTP_201_CREATED)

    @swagger_auto_schema(
        method="post",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                "guest_email": openapi.Schema(type=openapi.TYPE_STRING, description="Guest email"),
                "guest_identifier": openapi.Schema(type=openapi.TYPE_STRING, description="Guest identifier")
            },
            required=["guest_email", "guest_identifier"]
        ),
        responses={200: BookingSerializer}
    )
    @action(methods=["post"], detail=False, url_path="by-guest", permission_classes=[AllowAny])
    def by_guest(self, request):
        guest_email = request.data.get("guest_email")
        guest_identifier = request.data.get("guest_identifier")
        if not guest_email or not guest_identifier:
            return Response({"detail": "guest_email and guest_identifier required"}, status=400)
        try:
            booking = Booking.objects.get(
                guest_email=guest_email, guest_identifier=guest_identifier, customer__isnull=True)
            return Response(BookingSerializer(booking).data)
        except Booking.DoesNotExist:
            return Response({"detail": "Booking not found"}, status=404)

    #
    # @swagger_auto_schema(
    #     method="post",
    #     request_body=openapi.Schema(
    #         type=openapi.TYPE_OBJECT,
    #         properties={
    #             "driver_profile_id": openapi.Schema(type=openapi.TYPE_STRING, description="ID of the driver profile")
    #         },
    #         required=["driver_profile_id"]
    #     ),
    #     responses={200: BookingSerializer}
    # )
    @action(methods=["post"], detail=True, url_path="assign-driver")
    def assign_driver(self, request, pk=None):
        booking = self.get_object()
        driver_id = request.data.get("driver_profile_id")
        if not driver_id:
            return Response({"detail": "driver_profile_id required"}, status=400)
        booking.driver_id = driver_id
        booking.status = BookingStatus.ASSIGNED
        booking.save(update_fields=["driver_id", "status", "updated_at"])
        return Response(BookingSerializer(booking).data)

    @swagger_auto_schema(
        method="post",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                "status": openapi.Schema(type=openapi.TYPE_STRING, description="New booking status")
            },
            required=["status"]
        ),
        responses={200: openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                "id": openapi.Schema(type=openapi.TYPE_STRING),
                "status": openapi.Schema(type=openapi.TYPE_STRING)
            }
        )}
    )
    @action(methods=["post"], detail=True, url_path="set-status")
    def set_status(self, request, pk=None):
        booking = self.get_object()
        status_value = request.data.get("status")
        if status_value not in BookingStatus.values:
            return Response({"detail": "Invalid status"}, status=400)
        booking.status = status_value
        booking.updated_at = timezone.now()
        booking.save(update_fields=["status", "updated_at"])
        return Response({"id": str(booking.id), "status": booking.status})

    @swagger_auto_schema(
        method="get",
        responses={200: RecurringScheduleSerializer(many=True)}
    )
    @action(methods=["get"], detail=False, url_path="recurring")
    def recurring_list(self, request):
        qs = RecurringSchedule.objects.filter(customer=request.user)
        return Response(RecurringScheduleSerializer(qs, many=True).data)

    @action(methods=["post"], detail=False, url_path="recurring")
    @atomic
    def recurring_create(self, request):
        serializer = RecurringScheduleSerializer(
            data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        obj = serializer.save()
        return Response(RecurringScheduleSerializer(obj).data, status=201)


class BookingStatusView(APIView):
    """
    API endpoint to fetch all available booking statuses.

    This view returns a list of booking status choices defined in the BookingStatus model.
    It is accessible to anyone (AllowAny permission) and is useful for frontend dropdowns or filters.

    Response format:
    [
        {"value": "pending", "label": "Pending"},
        {"value": "scheduled", "label": "Scheduled"},
        ...
    ]
    """
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        responses={200: openapi.Schema(
            type=openapi.TYPE_ARRAY,
            items=openapi.Schema(
                type=openapi.TYPE_OBJECT,
                properties={
                    "value": openapi.Schema(type=openapi.TYPE_STRING),
                    "label": openapi.Schema(type=openapi.TYPE_STRING)
                }
            )
        )}
    )
    def get(self, request):
        statuses = [
            {"value": value, "label": label}
            for value, label in BookingStatus.choices
        ]
        return Response(statuses, status=status.HTTP_200_OK)


class ShippingTypeViewSet(viewsets.ModelViewSet):
    queryset = ShippingType.objects.all()
    serializer_class = ShippingTypeSerializer

    def get_permissions(self):
        if self.action in ["list", "retrieve"]:  # anyone can read
            return [IsAuthenticatedOrReadOnly()]
        if self.action in ["create", "update", "partial_update", "destroy"]:
            return [IsAdminOrReadOnly()]
        return super().get_permissions()


class ServiceTypeViewSet(viewsets.ModelViewSet):
    queryset = ServiceType.objects.all()
    serializer_class = ServiceTypeSerializer

    def get_permissions(self):
        if self.action in ["list", "retrieve"]:  # anyone can read
            return [IsAuthenticatedOrReadOnly()]
        if self.action in ["create", "update", "partial_update", "destroy"]:
            return [IsAdminOrReadOnly()]
        return super().get_permissions()
