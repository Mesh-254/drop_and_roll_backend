from decimal import Decimal

from drf_yasg import openapi  # type: ignore
from drf_yasg.utils import swagger_auto_schema  # type: ignore
from .tasks import send_booking_confirmation_email
from rest_framework import viewsets, status  # type: ignore
from rest_framework.decorators import action, api_view, permission_classes  # type: ignore
from django.shortcuts import get_object_or_404  # type: ignore
from rest_framework.permissions import IsAuthenticated, IsAuthenticatedOrReadOnly  # type: ignore
from rest_framework.response import Response  # type: ignore
from rest_framework.permissions import AllowAny  # type: ignore
from rest_framework.views import APIView  # type: ignore
from django.db.transaction import atomic  # type: ignore
from django.db import transaction  # type: ignore
import logging  # type: ignore
from driver.permissions import IsDriver  # type: ignore
from tracking.models import ProofOfDelivery


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
from .utils.utils import (
    format_datetime,
    format_address,
    get_current_location,
    build_tracking_timeline,
)

from django.db.models import Case, When, IntegerField  # type: ignore

logger = logging.getLogger(__name__)


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
        status_filter = self.request.query_params.get(
            "status", "")  # From frontend (e.g., statusParam in JSX)

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
            qs = qs.filter(guest_email=guest_email.lower(),
                           customer__isnull=True)
        else:
            # Default: empty queryset for unauthenticated users without guest_email
            return qs.none()

        # Apply status filter if provided (after user filters)
        if status_filter:
            qs = qs.filter(status=status_filter)

        # Hybrid ordering: Annotate status priority (lower number = higher priority)
        qs = qs.annotate(
            status_priority=Case(
                # Highest: New assignments
                When(status=BookingStatus.ASSIGNED, then=0),
                # Next: Ready to transit
                When(status=BookingStatus.PICKED_UP, then=1),
                When(status=BookingStatus.IN_TRANSIT,
                     then=2),    # Active: In progress
                When(status=BookingStatus.DELIVERED,
                     then=3),     # Lower: Completed
                When(status=BookingStatus.SCHEDULED,
                     then=4),     # Upcoming or pending
                # Others (e.g., CANCELLED, FAILED) at bottom
                default=5,
                output_field=IntegerField()
            )
            # Status priority asc, then most recent updates first
        ).order_by('status_priority', '-updated_at')

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

        # ADD: Immutability check (same as update_status)
        if booking.status == BookingStatus.DELIVERED:
            pod_exists = ProofOfDelivery.objects.filter(
                booking=booking).exists()
            if pod_exists:
                return Response({
                    'code': 'IMMUTABLE_DELIVERY',
                    'detail': 'Delivery completed with POD submitted. Status cannot be changed.',
                }, status=status.HTTP_403_FORBIDDEN)

        if status_value != BookingStatus.DELIVERED and booking.status == BookingStatus.DELIVERED:
            return Response({
                'code': 'REVERT_FORBIDDEN',
                'detail': 'Cannot revert delivered booking.',
            }, status=status.HTTP_400_BAD_REQUEST)

        # Proceed with update
        booking.status = status_value
        booking.updated_at = timezone.now()
        booking.save(update_fields=["status", "updated_at"])
        logger.info(
            f"Set status: Booking {booking.id} to {status_value} by {request.user.id}")
        return Response({"id": str(booking.id), "status": booking.status})

    # NEW: Proactive check endpoint (GET for single job)
    @action(methods=["get"], detail=True, url_path="check-immutable", permission_classes=[IsAuthenticated, IsDriver])
    def check_immutable(self, request, pk=None):
        booking = self.get_object()
        immutable = False
        reason = None
        if booking.status == BookingStatus.DELIVERED:
            pod_exists = ProofOfDelivery.objects.filter(
                booking=booking).exists()
            if pod_exists:
                immutable = True
                reason = "POD submitted - cannot update"
        elif booking.status == BookingStatus.DELIVERED:  # Revert block
            immutable = True
            reason = "Cannot revert delivered booking"
        return Response({"immutable": immutable, "reason": reason})

    @action(methods=["post"], detail=False, url_path="bulk-check-immutable", permission_classes=[IsAuthenticated, IsDriver])
    def bulk_check_immutable(self, request):
        job_ids = request.data.get('ids', [])
        if not job_ids:
            return Response({'error': 'No IDs provided'}, status=400)
        checks = {}
        for job_id in job_ids:
            try:
                booking = Booking.objects.get(
                    id=job_id, driver=request.user.driver_profile)
                immutable = False
                reason = None
                if booking.status == BookingStatus.DELIVERED:
                    pod_exists = ProofOfDelivery.objects.filter(
                        booking=booking).exists()
                    if pod_exists:
                        immutable = True
                        reason = "POD submitted - cannot update"
                checks[str(job_id)] = {
                    'immutable': immutable, 'reason': reason}
            except Booking.DoesNotExist:
                checks[str(job_id)] = {
                    'immutable': True, 'reason': 'Not found'}
        return Response(checks)

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

    # For bulk (array of {booking_id, new_status})

    @action(detail=False, methods=['post'], url_path='bulk-update-status', permission_classes=[IsAuthenticated, IsDriver])
    def bulk_update_status(self, request):
        updates = request.data.get('updates', [])  # [{booking_id, new_status}]
        if not updates:
            return Response({'error': 'No updates provided'}, status=status.HTTP_400_BAD_REQUEST)

        results = {'success': [], 'skipped': [], 'errors': []}
        with transaction.atomic():
            for update in updates:
                booking_id = update.get('booking_id')
                new_status = update.get('new_status')

                try:
                    booking = Booking.objects.get(
                        id=booking_id, driver=request.user.driver_profile)

                    # Same validation as single
                    if booking.status == BookingStatus.DELIVERED:
                        pod_exists = ProofOfDelivery.objects.filter(
                            booking=booking).exists()
                        if pod_exists:
                            results['skipped'].append({
                                'booking_id': booking_id,
                                'reason': 'POD submitted - immutable'
                            })
                            continue

                    if new_status != BookingStatus.DELIVERED and booking.status == BookingStatus.DELIVERED:
                        results['skipped'].append({
                            'booking_id': booking_id,
                            'reason': 'Cannot revert delivered'
                        })
                        continue

                    # Update
                    booking.status = new_status
                    booking.save()
                    results['success'].append(booking_id)
                    logger.info(
                        f"Bulk: Driver {request.user.id} updated {booking_id} to {new_status}")

                except Booking.DoesNotExist:
                    results['errors'].append(
                        {'booking_id': booking_id, 'reason': 'Not found'})

        message = f"Updated {len(results['success'])}/{len(updates)} jobs. Skipped {len(results['skipped'])}."
        if results['skipped']:
            message += " Some deliveries have POD submitted and cannot be changed."

        return Response({'detail': message, 'results': results}, status=status.HTTP_200_OK if results['success'] else status.HTTP_400_BAD_REQUEST)


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


# FUNCTION TO TRACK PARCELS


@api_view(["GET"])
@permission_classes([AllowAny])
def track_parcel(request):
    """
    Public tracking endpoint – no auth, no guest email.
    Query: ?tracking_number=BK-ABC123
    """
    tracking_number = request.query_params.get("tracking_number", "").strip().upper()

    if not tracking_number:
        return Response(
            {"error": "tracking_number is required"},
            status=400,
        )

    # --------------------------------------------------------------
    # 1. Efficient query – ONLY the fields we need
    # --------------------------------------------------------------
    try:
        booking = (
            Booking.objects.select_related(
                "pickup_address",           # Full address object
                "dropoff_address",          # Full address object
                "quote",                    # We need a few scalar fields from Quote
            )
            .only(
                # Booking
                "id",
                "tracking_number",
                "status",
                "scheduled_pickup_at",
                "scheduled_dropoff_at",
                "created_at",
                "updated_at",
                "final_price",
                # Address (city/region only – enough for UI)
                "pickup_address__city",
                "pickup_address__region",
                "dropoff_address__city",
                "dropoff_address__region",
                # Quote scalars
                "quote__weight_kg",
                "quote__fragile",
            )
            .get(tracking_number=tracking_number)
        )
    except Booking.DoesNotExist:
        return Response(
            {"error": "Tracking number not found"},
            status=404,
        )

    # --------------------------------------------------------------
    # 2. Build response payload (pure Python – no extra DB hits)
    # --------------------------------------------------------------
    timeline = build_tracking_timeline(booking)

    payload = {
        "tracking_number": booking.tracking_number,
        "status": booking.status,
        "current_location": get_current_location(booking),
        "estimated_delivery": format_datetime(booking.scheduled_dropoff_at),
        "origin": format_address(booking.pickup_address),
        "destination": format_address(booking.dropoff_address),
        "weight_kg": float(booking.quote.weight_kg) if booking.quote else None,
        "fragile": booking.quote.fragile if booking.quote else False,
        "final_price": float(booking.final_price),
        "timeline": timeline,
        "last_updated": format_datetime(booking.updated_at),
    }

    # --------------------------------------------------------------
    # 3. Optional: cache frequent lookups (Redis / in-memory)
    # --------------------------------------------------------------
    # from django.core.cache import cache
    # cache.set(f"track:{tracking_number}", payload, timeout=60)

    return Response(payload, status=200)