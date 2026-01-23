from decimal import Decimal

from django.http import HttpResponse
import qrcode
from io import BytesIO
from drf_yasg import openapi  # type: ignore
from drf_yasg.utils import swagger_auto_schema
from driver.models import DriverProfile, DriverShift
from driver.serializers import DriverProfileSerializer  # type: ignore
from .tasks import optimize_bookings, send_booking_confirmation_email
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

# from qr_code.qrcode.utils import ContactDetail, WifiConfig   # not needed here, just showing imports

from django.conf import settings  # type: ignore


import uuid
import shortuuid  # type: ignore
from django.utils import timezone  # type: ignore
from payments.models import PaymentTransaction, PaymentStatus
from payments.serializers import PaymentTransactionSerializer

from .models import (
    Quote,
    Booking,
    RecurringSchedule,
    BookingStatus,
    ShippingType,
    ServiceType,
    Route,
)
from .models import (
    Quote,
    Booking,
    RecurringSchedule,
    BookingStatus,
    ShippingType,
    ServiceType,
)
from .permissions import IsCustomer, IsAdminOrReadOnly, IsDriverOrAdmin
from .serializers import (
    QuoteRequestSerializer,
    QuoteSerializer,
    BookingCreateSerializer,
    BookingSerializer,
    RecurringScheduleSerializer,
    ShippingTypeSerializer,
    ServiceTypeSerializer,
    RouteSerializer,
    RecurringScheduleSerializer,
    ShippingTypeSerializer,
    ServiceTypeSerializer,
)
from .utils.pricing import compute_quote
from .utils.utils import (
    format_datetime,
    format_address,
    get_current_location,
    build_tracking_timeline,
)

from django.db.models import Case, When, IntegerField, Q  # type: ignore

logger = logging.getLogger(__name__)


class QuoteViewSet(viewsets.GenericViewSet):
    queryset = Quote.objects.all()
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        method="post",
        request_body=QuoteRequestSerializer,
        responses={201: QuoteSerializer},
    )
    @action(
        methods=["post"],
        detail=False,
        url_path="compute",
        permission_classes=[AllowAny],
    )
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
    queryset = Booking.objects.select_related(
        "pickup_address", "dropoff_address", "customer", "driver", "quote"
    ).prefetch_related("quote__shipping_type", "quote__service_type")
    serializer_class = BookingSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        guest_email = self.request.query_params.get("guest_email", "").lower()
        status_filter = self.request.query_params.get(
            "status", ""
        )  # From frontend (e.g., statusParam in JSX)

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
                # Highest: New assignments
                When(status=BookingStatus.ASSIGNED, then=0),
                # Next: Ready to transit
                When(status=BookingStatus.PICKED_UP, then=1),
                When(status=BookingStatus.IN_TRANSIT, then=2),  # Active: In progress
                When(status=BookingStatus.DELIVERED, then=3),  # Lower: Completed
                When(status=BookingStatus.SCHEDULED, then=4),  # Upcoming or pending
                # Others (e.g., CANCELLED, FAILED) at bottom
                default=5,
                output_field=IntegerField(),
            )
            # Status priority asc, then most recent updates first
        ).order_by("status_priority", "-updated_at")

        return qs

    def get_object(self):
        # Use get_queryset to ensure proper filtering
        queryset = self.get_queryset()
        obj = super().get_object()  # This will raise 404 if no object is found
        return obj

    def get_permissions(self):
        if self.action in [
            "create",
            "by_guest",
            "bulk_upload",
            "recurring_list",
            "recurring_create",
            "retrieve",
        ]:
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
                guest_email=guest_email,
                customer__isnull=True,
                status=BookingStatus.PENDING,
            ).count()

        # if pending_count >= 5:  # Anti-spam limit
        #     raise ValidationError("Too many pending bookings.")

        # Save the booking using serializer's logic
        booking = serializer.save(
            status=BookingStatus.PENDING,
            payment_expires_at=timezone.now() + timezone.timedelta(days=1),
        )

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
            reference=str(uuid.uuid4())[:12].replace("-", ""),
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
                "guest_email": openapi.Schema(
                    type=openapi.TYPE_STRING, description="Guest email"
                ),
                "guest_identifier": openapi.Schema(
                    type=openapi.TYPE_STRING, description="Guest identifier"
                ),
            },
            required=["guest_email", "guest_identifier"],
        ),
        responses={200: BookingSerializer},
    )
    @action(
        methods=["post"],
        detail=False,
        url_path="by-guest",
        permission_classes=[AllowAny],
    )
    def by_guest(self, request):
        guest_email = request.data.get("guest_email")
        guest_identifier = request.data.get("guest_identifier")
        if not guest_email or not guest_identifier:
            return Response(
                {"detail": "guest_email and guest_identifier required"}, status=400
            )
        try:
            booking = Booking.objects.get(
                guest_email=guest_email,
                guest_identifier=guest_identifier,
                customer__isnull=True,
            )
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
        # booking.status = BookingStatus.ASSIGNED
        booking.save(update_fields=["driver_id", "status", "updated_at"])
        return Response(BookingSerializer(booking).data)

    @swagger_auto_schema(
        method="post",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                "status": openapi.Schema(
                    type=openapi.TYPE_STRING, description="New booking status"
                )
            },
            required=["status"],
        ),
        responses={
            200: openapi.Schema(
                type=openapi.TYPE_OBJECT,
                properties={
                    "id": openapi.Schema(type=openapi.TYPE_STRING),
                    "status": openapi.Schema(type=openapi.TYPE_STRING),
                },
            )
        },
    )
    @action(methods=["post"], detail=True, url_path="set-status")
    def set_status(self, request, pk=None):
        booking = self.get_object()
        status_value = request.data.get("status")
        if status_value not in BookingStatus.values:
            return Response({"detail": "Invalid status"}, status=400)

        # ADD: Immutability check (same as update_status)
        if booking.status == BookingStatus.DELIVERED:
            pod_exists = ProofOfDelivery.objects.filter(booking=booking).exists()
            if pod_exists:
                return Response(
                    {
                        "code": "IMMUTABLE_DELIVERY",
                        "detail": "Delivery completed with POD submitted. Status cannot be changed.",
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

        if (
            status_value != BookingStatus.DELIVERED
            and booking.status == BookingStatus.DELIVERED
        ):
            return Response(
                {
                    "code": "REVERT_FORBIDDEN",
                    "detail": "Cannot revert delivered booking.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Proceed with update
        booking.status = status_value
        booking.updated_at = timezone.now()
        booking.save(update_fields=["status", "updated_at"])
        logger.info(
            f"Set status: Booking {booking.id} to {status_value} by {request.user.id}"
        )
        return Response({"id": str(booking.id), "status": booking.status})

    # NEW: Proactive check endpoint (GET for single job)
    @action(
        methods=["get"],
        detail=True,
        url_path="check-immutable",
        permission_classes=[IsAuthenticated, IsDriver],
    )
    def check_immutable(self, request, pk=None):
        booking = self.get_object()
        immutable = False
        reason = None
        if booking.status == BookingStatus.DELIVERED:
            pod_exists = ProofOfDelivery.objects.filter(booking=booking).exists()
            if pod_exists:
                immutable = True
                reason = "POD submitted - cannot update"
        elif booking.status == BookingStatus.DELIVERED:  # Revert block
            immutable = True
            reason = "Cannot revert delivered booking"
        return Response({"immutable": immutable, "reason": reason})

    @action(
        methods=["post"],
        detail=False,
        url_path="bulk-check-immutable",
        permission_classes=[IsAuthenticated, IsDriver],
    )
    def bulk_check_immutable(self, request):
        job_ids = request.data.get("ids", [])
        if not job_ids:
            return Response({"error": "No IDs provided"}, status=400)
        checks = {}
        for job_id in job_ids:
            try:
                booking = Booking.objects.get(
                    id=job_id, driver=request.user.driver_profile
                )
                immutable = False
                reason = None
                if booking.status == BookingStatus.DELIVERED:
                    pod_exists = ProofOfDelivery.objects.filter(
                        booking=booking
                    ).exists()
                    if pod_exists:
                        immutable = True
                        reason = "POD submitted - cannot update"
                checks[str(job_id)] = {"immutable": immutable, "reason": reason}
            except Booking.DoesNotExist:
                checks[str(job_id)] = {"immutable": True, "reason": "Not found"}
        return Response(checks)

    @swagger_auto_schema(
        method="get", responses={200: RecurringScheduleSerializer(many=True)}
    )
    @action(methods=["get"], detail=False, url_path="recurring")
    def recurring_list(self, request):
        qs = RecurringSchedule.objects.filter(customer=request.user)
        return Response(RecurringScheduleSerializer(qs, many=True).data)

    @action(methods=["post"], detail=False, url_path="recurring")
    @atomic
    def recurring_create(self, request):
        serializer = RecurringScheduleSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        obj = serializer.save()
        return Response(RecurringScheduleSerializer(obj).data, status=201)

    # For bulk (array of {booking_id, new_status})

    @action(
        detail=False,
        methods=["post"],
        url_path="bulk-update-status",
        permission_classes=[IsAuthenticated, IsDriver],
    )
    def bulk_update_status(self, request):
        updates = request.data.get("updates", [])  # [{booking_id, new_status}]
        if not updates:
            return Response(
                {"error": "No updates provided"}, status=status.HTTP_400_BAD_REQUEST
            )

        results = {"success": [], "skipped": [], "errors": []}
        with transaction.atomic():
            for update in updates:
                booking_id = update.get("booking_id")
                new_status = update.get("new_status")

                try:
                    booking = Booking.objects.get(
                        id=booking_id, driver=request.user.driver_profile
                    )

                    # Same validation as single
                    if booking.status == BookingStatus.DELIVERED:
                        pod_exists = ProofOfDelivery.objects.filter(
                            booking=booking
                        ).exists()
                        if pod_exists:
                            results["skipped"].append(
                                {
                                    "booking_id": booking_id,
                                    "reason": "POD submitted - immutable",
                                }
                            )
                            continue

                    if (
                        new_status != BookingStatus.DELIVERED
                        and booking.status == BookingStatus.DELIVERED
                    ):
                        results["skipped"].append(
                            {
                                "booking_id": booking_id,
                                "reason": "Cannot revert delivered",
                            }
                        )
                        continue

                    # Update
                    booking.status = new_status
                    booking.save()
                    results["success"].append(booking_id)
                    logger.info(
                        f"Bulk: Driver {request.user.id} updated {booking_id} to {new_status}"
                    )

                except Booking.DoesNotExist:
                    results["errors"].append(
                        {"booking_id": booking_id, "reason": "Not found"}
                    )

        message = f"Updated {len(results['success'])}/{len(updates)} jobs. Skipped {len(results['skipped'])}."
        if results["skipped"]:
            message += " Some deliveries have POD submitted and cannot be changed."

        return Response(
            {"detail": message, "results": results},
            status=(
                status.HTTP_200_OK
                if results["success"]
                else status.HTTP_400_BAD_REQUEST
            ),
        )


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
        responses={
            200: openapi.Schema(
                type=openapi.TYPE_ARRAY,
                items=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        "value": openapi.Schema(type=openapi.TYPE_STRING),
                        "label": openapi.Schema(type=openapi.TYPE_STRING),
                    },
                ),
            )
        }
    )
    def get(self, request):
        statuses = [
            {"value": value, "label": label} for value, label in BookingStatus.choices
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
                "pickup_address",  # Full address object
                "dropoff_address",  # Full address object
                "quote",  # We need a few scalar fields from Quote
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


class RouteViewSet(viewsets.ModelViewSet):
    queryset = Route.objects.all()
    serializer_class = RouteSerializer
    permission_classes = [IsAdminOrReadOnly]

    @action(detail=False, methods=["post"], permission_classes=[IsAdminOrReadOnly])
    def optimize_now(self, request):
        optimize_bookings.delay()  # Trigger manually
        return Response({"status": "Optimization queued"})

    # NEW: Action to get available drivers for a specific route (filtered by hub, active, and availability)
    # Line-by-line details:
    # 1. Decorator: Defines a detail=True action (requires pk for route), GET method, admin-only.
    @action(detail=True, methods=["get"], permission_classes=[IsAdminOrReadOnly])
    def get_available_drivers(self, request, pk=None):
        route = self.get_object()
        drivers = DriverProfile.objects.filter(
            hub=route.hub, status="active"  # Same hub  # Assuming your model has status
        ).select_related(
            "shift"
        )  # Optimize

        available = []
        for d in drivers:
            shift = DriverShift.get_or_create_today(d)
            current = shift.current_load or {"weight": 0.0, "volume": 0.0, "hours": 0.0}
            remaining_weight = (
                shift.max_weight - current["weight"]
            )  # Assume max_weight field
            remaining_volume = shift.max_volume - current["volume"]

            if route.leg_type == "mixed":
                net_weight = 0.0
                net_volume = 0.0
                for b in route.bookings.all():
                    typ = route.get_stop_type(b)
                    sign = 1 if typ == "pickup" else -1
                    net_weight += sign * float(b.quote.weight_kg or 0)
                    net_volume += sign * float(b.quote.volume_m3 or 0)
                if remaining_weight >= net_weight and remaining_volume >= net_volume:
                    available.append(d)
            else:
                # Non-mixed: Simple positive sum check (original logic)
                total_weight = sum(
                    float(b.quote.weight_kg or 0) for b in route.bookings.all()
                )
                total_volume = sum(
                    float(b.quote.volume_m3 or 0) for b in route.bookings.all()
                )
                if (
                    remaining_weight >= total_weight
                    and remaining_volume >= total_volume
                ):
                    available.append(d)

        serializer = DriverProfileSerializer(available, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], permission_classes=[IsAdminOrReadOnly])
    def assign_driver(self, request, pk=None):
        route = self.get_object()
        driver_id = request.data.get("driver_id")

        if not driver_id:
            return Response(
                {"error": "driver_id required"}, status=status.HTTP_400_BAD_REQUEST
            )

        driver = get_object_or_404(DriverProfile, id=driver_id)

        # Check if driver available, etc. (add your business rules if missing)

        with transaction.atomic():
            route.driver = driver
            if route.shift:
                route.shift.driver = driver
                route.shift.status = DriverShift.Status.ASSIGNED
                # Update load if needed (similar to signals)
                route.shift.save(update_fields=["driver", "status"])

            # Updated: Bookings with mixed support
            if route.leg_type == "mixed":
                updated = 0
                for booking in route.bookings.all():
                    typ = route.get_stop_type(booking)
                    booking_status = (
                        BookingStatus.ASSIGNED
                        if typ == "pickup"
                        else BookingStatus.IN_TRANSIT
                    )
                    booking.driver = driver
                    booking.hub = driver.hub
                    booking.updated_at = timezone.now()
                    booking.status = booking_status
                    booking.save()
                    updated += 1
            else:
                booking_status = (
                    BookingStatus.ASSIGNED
                    if route.leg_type == "pickup"
                    else (
                        BookingStatus.IN_TRANSIT
                        if route.leg_type == "delivery"
                        else BookingStatus.ASSIGNED
                    )  # fallback
                )
                updated = route.bookings.update(
                    driver=driver,
                    hub=driver.hub,
                    updated_at=timezone.now(),
                    status=booking_status,
                )

            # Re-save route for validation (your NEW comment)
            route.save()

            logger.info(
                f"Admin manually assigned Route {route.id} to Driver {driver.user.get_full_name()} "
                f"({driver.user.email}). Updated {updated} bookings."
            )

        return Response(
            {
                "success": True,
                "route_id": str(route.id),
                "driver": driver.user.get_full_name(),
                "bookings_updated": updated,
                "new_booking_status": (
                    booking_status
                    if "booking_status" in locals()
                    else "mixed (per-type)"
                ),
                "shift_assigned": (
                    route.shift.driver_id is not None if route.shift else False
                ),
            }
        )

    @action(detail=True, methods=["get"], permission_classes=[IsDriver])
    def get_route_details(self, request, pk=None):
        route = self.get_object()
        if route.driver.user != request.user:
            return Response(
                {"error": "Not your route"}, status=status.HTTP_403_FORBIDDEN
            )

        # Use detailed_stops (includes type from serializers update)
        details = route.get_detailed_stops(for_admin=False)
        for stop in details:
            stop["qr_prompt"] = (
                True if stop["type"] in ["pickup", "delivery"] else False
            )  # Prompt for scan

        return Response(
            {
                "route_id": str(route.id),
                "leg_type": route.leg_type,
                "total_hours": route.total_time_hours,
                "total_km": route.total_distance_km,
                "stops": details,
            }
        )


class BookingQRCodeView(APIView):
    permission_classes = [IsAuthenticated]  # Or IsDriver/IsCustomer

    def get(self, request, pk):
        booking = get_object_or_404(Booking, pk=pk)
        if not booking.qr_code_url:
            booking.generate_qr()

        # Generate on-the-fly as fallback or always
        code = booking.assigned_qr_code or booking.tracking_number or str(booking.id)
        qr_content = f"{settings.FRONTEND_URL}/track/{code}"

        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=4,
        )
        qr.add_data(qr_content)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")

        response = HttpResponse(content_type="image/png")
        img.save(response, "PNG")
        return response


# Un-comment + enhance booking_qr_code (similar fallback)
def booking_qr_code(request, pk):
    """
    Function-based view to serve a QR code image for a booking.

    - Generates on-the-fly if qr_code_url is missing.
    - Supports query params: ?size=M/L/H & ?format=png/svg
    - Uses assigned_qr_code if set (for random QR adoption), else tracking_number or ID.
    - Returns PNG or SVG based on format.
    - Logs generation for debugging.
    """
    booking = get_object_or_404(Booking, pk=pk)

    # Fallback: Generate if no stored URL
    if not booking.qr_code_url:
        try:
            booking.generate_qr(force_regenerate=True)
            logger.info(f"Generated QR on-the-fly for Booking {booking.id}")
        except Exception as e:
            logger.error(f"Failed to generate QR for Booking {booking.id}: {e}")
            return HttpResponse("QR generation failed", status=500)

    # Determine QR content (prefer assigned_qr_code if set)
    code = booking.assigned_qr_code or booking.tracking_number or str(booking.id)
    qr_content = f"{settings.FRONTEND_URL}/track/{code}"

    # Get params from query string
    size_param = request.GET.get("size", "M").upper()  # M, L, H
    format_param = request.GET.get("format", "png").lower()

    # Map size to box_size (adjust as needed)
    size_map = {"M": 10, "L": 15, "H": 20}
    box_size = size_map.get(size_param, 10)  # Default M

    # Generate QR
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=box_size,
        border=4,
    )
    qr.add_data(qr_content)
    qr.make(fit=True)

    # Create image
    img = qr.make_image(fill_color="black", back_color="white")

    # Prepare response
    if format_param == "svg":
        # SVG output (requires qrcode[pil] + svgwrite or similar, but qrcode doesn't support SVG natively)
        # Fallback to PNG if SVG not supported — or install qrcode-svg if needed
        content_type = "image/png"
        extension = "png"
        buffer = BytesIO()
        img.save(buffer, format="PNG")
    else:
        content_type = "image/png"
        extension = "png"
        buffer = BytesIO()
        img.save(buffer, format="PNG")

    buffer.seek(0)

    response = HttpResponse(buffer, content_type=content_type)
    response["Content-Disposition"] = (
        f'inline; filename="booking_{booking.id}_qr.{extension}"'
    )
    return response


# New: Scan API
@api_view(["POST"])
@permission_classes([IsDriver])
def scan_qr(request):
    """
    API endpoint for drivers to scan QR codes on parcels during pickup or delivery.

    Supported flows:
    1. Original QR (from customer email/print):
       - Parses URL → finds booking by tracking_number or assigned_qr_code
       - Validates driver owns the route
       - Updates status (PICKED_UP for pickup stops, DELIVERED for delivery stops)
    2. Random QR (pre-printed by driver, when customer forgot to print):
       - If no match → treats qr_content as new unique code
       - Checks if code already used (uniqueness)
       - Assigns to booking.assigned_qr_code
       - Re-generates qr_code_url with new code
       - Updates status (same as above)

    Request body (JSON):
    {
        "qr_content": "https://yourdomain/track/ABC123"   // or plain code "abc123def456"
        "booking_id": "550e8400-e29b-41d4-a716-446655440000"  // required for context
    }

    Response (200 OK):
    {
        "success": true,
        "booking_id": "550e8400-e29b-41d4-a716-446655440000",
        "new_status": "picked_up"  // or "delivered"
    }

    Errors:
    - 400: Missing/invalid qr_content or booking_id
    - 403: Not the driver's route/booking
    - 400: Invalid stop type
    - 400: Random code already assigned
    - 500: Internal error (logged)

    Security: Only authenticated drivers can call.
    Atomicity: All DB writes in one transaction.
    """
    # 1. Extract required fields from request
    qr_content = request.data.get("qr_content")
    booking_id = request.data.get("booking_id")

    if not qr_content:
        return Response({"error": "qr_content is required"}, status=400)

    if not booking_id:
        return Response({"error": "booking_id is required"}, status=400)

    # 2. Fetch booking and route — early validation
    booking = get_object_or_404(Booking, id=booking_id)

    # Get the route this booking belongs to (assume one route per booking)
    route = booking.route_set.first()
    if not route:
        return Response({"error": "Booking is not assigned to any route"}, status=400)

    # Ensure the current user (driver) owns this route
    if route.driver.user != request.user:
        return Response({"error": "You are not assigned to this route"}, status=403)

    # 3. Wrap everything in atomic transaction (critical for random assign + status update)
    with transaction.atomic():
        # 4. Parse the scanned QR content
        parsed = parse_qr_content(qr_content)

        if isinstance(parsed, str):  # ← Random code case (plain string)
            # Uniqueness check — prevent reuse of the same pre-printed code
            if Booking.objects.filter(assigned_qr_code=parsed).exists():
                return Response(
                    {
                        "error": "This random QR code is already assigned to another parcel"
                    },
                    status=400,
                )

            # Assign the random code to this booking
            booking.assigned_qr_code = parsed

            # Re-generate the QR image with the new code embedded in the URL
            booking.generate_qr(force_regenerate=True)

            # Save both fields
            booking.save(update_fields=["assigned_qr_code", "qr_code_url"])

            logger.info(
                f"Random QR code '{parsed}' assigned to Booking {booking.id} "
                f"by driver {request.user.get_full_name()} (ID: {request.user.id})"
            )

        elif (
            parsed and parsed == booking.id
        ):  # ← Valid match (original or previously assigned QR)
            logger.info(
                f"Valid QR scan for Booking {booking.id} by driver {request.user.get_full_name()}"
            )
            # No assignment needed — proceed to status update

        else:
            # Invalid QR content — tell app to prompt random scan
            return Response(
                {"error": "Invalid QR code - try scanning a random one?"}, status=400
            )

        # 5. Determine stop type and update booking status
        stop_type = route.get_stop_type(booking)

        if stop_type == "pickup":
            new_status = BookingStatus.PICKED_UP
        elif stop_type == "delivery":
            new_status = BookingStatus.DELIVERED
        else:
            return Response(
                {"error": f"Invalid stop type '{stop_type}' for this booking"},
                status=400,
            )

        # Apply status change
        booking.status = new_status
        booking.updated_at = timezone.now()
        booking.save(update_fields=["status", "updated_at"])

        # Log final success
        logger.info(
            f"QR scan successful for Booking {booking.id} "
            f"(type: {stop_type}, new status: {new_status}) "
            f"by driver {request.user.get_full_name()}"
        )

        # 6. Return success response
        return Response(
            {
                "success": True,
                "booking_id": str(booking.id),
                "new_status": new_status,
                "qr_url": booking.qr_code_url,  # Optional: return updated QR if regenerated
            },
            status=200,
        )


# Helper (add to views or utils.py)
def parse_qr_content(qr_content):
    # e.g., "https://site/track/ABC123" → "ABC123" → query Booking.tracking_number == "ABC123" → return id
    if "/track/" in qr_content:
        code = qr_content.split("/track/")[-1]
        try:
            # Check both tracking_number and assigned_qr_code
            booking = Booking.objects.get(
                Q(tracking_number=code) | Q(assigned_qr_code=code)
            )
            return booking.id
        except Booking.DoesNotExist:
            return None
    else:
        # Random code (plain string, e.g., 'abc123def')
        return qr_content  # Return code itself for assign check


# New: Regenerate QR for "random" cases
@api_view(["POST"])
@permission_classes([IsDriver])
def regenerate_qr(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id)
    route = booking.route_set.first()
    if not route or route.driver.user != request.user:
        return Response({"error": "Not your booking"}, status=403)

    new_url = booking.generate_qr(force_regenerate=True)
    return Response({"success": True, "new_qr_url": new_url})
