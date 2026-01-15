import uuid
from decimal import Decimal

from django.utils import timezone
from rest_framework import viewsets, mixins
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.views import APIView
from rest_framework import serializers
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from django.db.models import Avg, OuterRef, Subquery
from rest_framework.permissions import IsAdminUser
from django.db import transaction
from rest_framework.parsers import MultiPartParser, FormParser
from django.db.models import Case, When, IntegerField

from bookings.models import Booking, BookingStatus, Route
from bookings.serializers import RouteSerializer
from bookings.serializers import BookingSerializer
from driver.models import (
    DriverAvailability,
    DriverPayout,
    DriverRating,
    DriverDocument,
    DriverShift,
    DriverProfile,
    DriverDocument,
    DriverLocation,
)
from driver.serializers import (
    DriverAvailabilitySerializer,
    DriverLocationCreateSerializer,
    DriverPayoutSerializer,
    DriverPayoutCreateSerializer,
    DriverRatingSerializer,
    DriverDocumentSerializer,
    DriverInviteCreateSerializer,
    DriverInviteDetailSerializer,
    DriverInviteAcceptSerializer,
    DriverShiftSerializer,
    DriverLocationSerializer,
)
from users.serializers import UserSerializer
from .permissions import IsAdmin, IsDriver, IsCustomer
from datetime import timedelta


class DriverAvailabilityViewSet(
    mixins.CreateModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet
):
    queryset = DriverAvailability.objects.all()
    serializer_class = DriverAvailabilitySerializer
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        if self.action in ["list", "retrieve"]:
            return [IsAdmin()]  # Admins view all
        elif self.action in ["update", "partial_update", "me"]:
            return [IsDriver()]  # Drivers update own
        return super().get_permissions()

    # NEW: Action for driver's own availability
    @action(detail=False, methods=["get", "patch"], url_path="me")
    def me(self, request):
        try:
            availability = DriverAvailability.objects.get(
                driver_profile__user=request.user
            )
        except DriverAvailability.DoesNotExist:
            return Response(
                {"error": "Availability not found"}, status=status.HTTP_404_NOT_FOUND
            )

        if request.method == "GET":
            serializer = self.get_serializer(availability)
            return Response(serializer.data)

        elif request.method == "PATCH":
            serializer = self.get_serializer(
                availability, data=request.data, partial=True
            )
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return DriverAvailability.objects.none()
        return DriverAvailability.objects.select_related(
            "driver_profile", "driver_profile__user"
        )

    # @swagger_auto_schema(method="post", request_body=DriverAvailabilitySerializer, responses={200: DriverAvailabilitySerializer})
    def create(self, request, *args, **kwargs):
        profile = getattr(request.user, "driver_profile", None)
        if profile is None:
            return Response({"detail": "No driver profile"}, status=400)
        s = DriverAvailabilitySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        obj, _ = DriverAvailability.objects.update_or_create(
            driver_profile=profile,
            defaults={
                "available": s.validated_data.get("available", False),
                "lat": s.validated_data.get("lat"),
                "lng": s.validated_data.get("lng"),
                "last_updated": timezone.now(),
            },
        )
        # # Optionally update profile status
        # if obj.available and profile.status != DriverStatus.ACTIVE:
        #     profile.status = DriverStatus.ACTIVE
        #     profile.save(update_fields=["status"])
        # if not obj.available and profile.status == DriverStatus.ACTIVE:
        #     profile.status = DriverStatus.INACTIVE
        #     profile.save(update_fields=["status"])
        # return Response(DriverAvailabilitySerializer(obj).data)


class DriverPayoutViewSet(viewsets.ModelViewSet):
    serializer_class = DriverPayoutSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return DriverPayout.objects.none()
        u = self.request.user
        qs = DriverPayout.objects.select_related(
            "driver_profile", "driver_profile__user"
        )
        if getattr(u, "role", None) == "driver":
            return qs.filter(driver_profile__user=u)
        return qs  # admin

    def get_permissions(self):
        if self.action in ["create", "update", "partial_update", "destroy"]:
            return [IsAdmin()]
        if self.action in ["list", "retrieve"]:
            return []  # restricted by get_queryset
        return super().get_permissions()

    def get_serializer_class(self):
        if self.action == "create":
            return DriverPayoutCreateSerializer
        return DriverPayoutSerializer

    # @swagger_auto_schema(method="post", request_body=DriverPayoutCreateSerializer, responses={201: DriverPayoutSerializer})
    def create(self, request, *args, **kwargs):
        s = DriverPayoutCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        payout = s.save()
        return Response(DriverPayoutSerializer(payout).data, status=201)


class DriverRatingViewSet(
    mixins.CreateModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet
):
    serializer_class = DriverRatingSerializer

    def get_permissions(self):
        if self.action == "create":
            return [IsCustomer()]
        if self.action == "list":
            return []
        return super().get_permissions()

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return DriverRating.objects.none()
        qs = DriverRating.objects.select_related("driver_profile", "customer")
        driver_id = self.request.query_params.get("driver_profile")
        if driver_id:
            qs = qs.filter(driver_profile_id=driver_id)
        return qs

    # @swagger_auto_schema(method="post", request_body=DriverRatingSerializer, responses={201: DriverRatingSerializer})
    def create(self, request, *args, **kwargs):
        s = DriverRatingSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        rating = s.save()
        # Update aggregates on profile
        profile = rating.driver_profile
        # Simple running average
        new_count = profile.rating_count + 1
        new_avg = (
            profile.rating_avg * profile.rating_count + Decimal(rating.rating)
        ) / Decimal(new_count)
        profile.rating_count = new_count
        profile.rating_avg = new_avg.quantize(Decimal("0.01"))
        profile.save(update_fields=["rating_count", "rating_avg"])
        return Response(DriverRatingSerializer(rating).data, status=201)


class DriverDocumentViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = DriverDocumentSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def get_permissions(self):
        if self.action == "verify_document":
            return [IsAdminUser()]
        return [IsAuthenticated(), IsDriver()]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return DriverDocument.objects.none()
        user = self.request.user
        if user.role == "admin":
            return DriverDocument.objects.all()
        if hasattr(user, "driver_profile"):
            return user.driver_profile.documents.all()
        return DriverDocument.objects.none()

    @transaction.atomic
    def perform_create(self, serializer):
        if not hasattr(self.request.user, "driver_profile"):
            print("User has no driver profile:", self.request.user)
            raise serializers.ValidationError(
                {"detail": "User must have a driver profile to upload documents."}
            )
        driver = self.request.user.driver_profile
        doc_type = serializer.validated_data.get("doc_type")
        print("Uploading document of type:", doc_type)
        print("Driver ID:", driver.id)
        print("File info:", serializer.validated_data.get("file"))
        # Check if document already exists for this driver and doc_type
        existing_doc = DriverDocument.objects.filter(
            driver=driver, doc_type=doc_type
        ).first()
        if existing_doc:
            # Update existing document
            existing_doc.file = serializer.validated_data["file"]
            existing_doc.uploaded_at = timezone.now()
            existing_doc.verified = False  # Reset verification on new upload
            existing_doc.notes = None
            existing_doc.save()
            serializer.instance = existing_doc
        else:
            # Create new document
            serializer.save(driver=driver)

    @action(detail=True, methods=["post"], url_path="verify")
    @transaction.atomic
    def verify_document(self, request, pk=None):
        document = self.get_object()
        verified = request.data.get("verified", False)
        notes = request.data.get("notes", None)
        if document.verified == verified:
            return Response(
                {
                    "detail": f"Document is already {'verified' if verified else 'unverified'}"
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        document.verified = verified
        document.notes = notes if notes is not None else document.notes
        document.save()

        return Response(DriverDocumentSerializer(document).data)


class DriverInviteViewSet(
    mixins.CreateModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet
):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get_queryset(self):
        from .models import DriverInvitation

        return DriverInvitation.objects.all().order_by("-expires_at")

    def get_serializer_class(self):
        if self.action == "create":
            return DriverInviteCreateSerializer
        return DriverInviteDetailSerializer

    @action(
        methods=["post"], detail=False, url_path="accept", permission_classes=[AllowAny]
    )
    def accept(self, request):
        s = DriverInviteAcceptSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        user = s.save()
        return Response(UserSerializer(user).data, status=201)


class StandardPagination(PageNumberPagination):
    # Default items per page (adjust based on UI needs, e.g., 20-50)
    page_size = 10
    # Allow frontend to override, e.g., ?page_size=20
    page_size_query_param = "page_size"
    max_page_size = 100  # Prevent abuse


class DriverAssignedBookingViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = BookingSerializer
    permission_classes = [IsAuthenticated, IsDriver | IsAdmin]
    pagination_class = StandardPagination

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Booking.objects.none()

        user = self.request.user
        driver_id = self.request.query_params.get("driver_id")
        status_param = self.request.query_params.get("status")

        # Filter bookings for the authenticated driver
        qs = (
            Booking.objects.filter(driver__user=self.request.user)
            .select_related(
                "pickup_address", "dropoff_address", "customer", "driver", "quote"
            )
            .prefetch_related("quote__shipping_type", "quote__service_type")
        )

        # Apply status filter if provided (from frontend statusParam)
        status_filter = self.request.query_params.get("status", "")
        if status_filter:
            qs = qs.filter(status=status_filter)

        # Hybrid ordering: Annotate status priority (lower number = higher priority)
        qs = qs.annotate(
            status_priority=Case(
                When(status=BookingStatus.ASSIGNED, then=0),  # Highest: New assignments
                When(status=BookingStatus.PICKED_UP, then=1),  # Next: Ready to transit
                When(status=BookingStatus.IN_TRANSIT, then=2),  # Active: In progress
                When(status=BookingStatus.DELIVERED, then=3),  # Lower: Completed
                When(status=BookingStatus.SCHEDULED, then=4),  # Upcoming or pending
                default=5,  # Others (e.g., CANCELLED, FAILED) at bottom
                output_field=IntegerField(),
            )
        ).order_by(
            "status_priority", "-updated_at"
        )  # Status priority asc, then most recent updates first

        return qs.order_by("status_priority", "-updated_at")

    # def list(self, request, *args, **kwargs):
    #     # Rely on DRF's authentication and permission classes
    #     return super().list(request, *args, **kwargs)

    # @action(detail=False, methods=['get'], url_path='my-route')
    # def my_route(self, request):
    #     route = Route.objects.filter(driver=request.user.driver_profile, status='assigned').first()
    #     return Response({'ordered_stops': route.ordered_stops})  # Next stop is [0]


# class DriverRouteViewSet(viewsets.GenericViewSet):
#     permission_classes = [IsDriver]

#     @action(detail=False, methods=["get"], url_path="current-route")
#     def current_route(self, request):
#         """
#         Primary endpoint for driver app.
#         Returns current optimized route with bookings in STRICT VRP order.
#         Falls back gracefully if no route.
#         """
#         driver = request.user.driver_profile
#         if not driver:
#             return Response(
#                 {"detail": "No driver profile found"},
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         # Find active route (you can adjust status if needed)
#         route = (
#             Route.objects.filter(driver=driver, status="assigned")
#             .select_related("hub")
#             .order_by("-visible_at")
#             .first()
#         )

#         if not route or not route.ordered_stops:
#             # Fallback: return individual assigned bookings (not in route)
#             fallback_bookings = Booking.objects.filter(
#                 driver=driver,
#                 status__in=[
#                     BookingStatus.ASSIGNED,
#                     BookingStatus.PICKED_UP,
#                     BookingStatus.AT_HUB,
#                 ],
#             ).select_related("pickup_address", "dropoff_address")

#             # Apply same status priority as your old view
#             fallback_bookings = fallback_bookings.annotate(
#                 status_priority=Case(
#                     When(status=BookingStatus.ASSIGNED, then=0),
#                     When(status=BookingStatus.PICKED_UP, then=1),
#                     When(status=BookingStatus.AT_HUB, then=2),
#                     default=3,
#                     output_field=IntegerField(),
#                 )
#             ).order_by("status_priority", "-updated_at")

#             serializer = BookingSerializer(fallback_bookings, many=True)
#             return Response(
#                 {
#                     "route_id": None,
#                     "hub_name": None,
#                     "is_optimized_route": False,
#                     "total_stops": len(serializer.data),
#                     "ordered_bookings": serializer.data,
#                     "message": "No optimized route active. Showing individual assignments.",
#                 }
#             )

#         # === MAIN CASE: Optimized route exists ===
#         ordered_bookings = route.ordered_bookings  # Use your property
#         serializer = BookingSerializer(ordered_bookings, many=True)

#         # Optional: detect current "next" stop
#         next_stop_index = None
#         for i, booking in enumerate(ordered_bookings):
#             if booking.status in [BookingStatus.ASSIGNED, BookingStatus.PICKED_UP]:
#                 next_stop_index = i
#                 break

#         return Response(
#             {
#                 "route_id": str(route.id),
#                 "hub_name": route.hub.name if route.hub else "No Hub",
#                 "is_optimized_route": True,
#                 "total_distance_km": float(route.total_distance_km or 0),
#                 "total_time_hours": float(route.total_time_hours or 0),
#                 "total_stops": len(ordered_bookings),
#                 "next_stop_index": next_stop_index,
#                 "ordered_bookings": serializer.data,  # Strict VRP order
#             }
#         )


class DriverRouteViewSet(viewsets.GenericViewSet):
    permission_classes = [IsDriver]
    pagination_class = PageNumberPagination

    @action(detail=False, methods=["get"], url_path="current-route")
    def current_route(self, request):
        """
        Unified endpoint for driver's current work: combines optimized routes (VRP) and manual assignments.
        - If no status filter: Prioritizes active route (strict order), appends manual bookings.
        - If status filter: Shows all bookings with that status (route or manual), ordered by priority.
        Supports pagination for large lists.
        """
        driver = DriverProfile.objects.select_related("hub", "hub__address").get(
            user=request.user
        )
        if not driver:
            return Response(
                {"detail": "No driver profile found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Parse query params
        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", 10))
        status_filter = request.query_params.get("status", "")
        active_statuses = [
            BookingStatus.ASSIGNED,
            BookingStatus.AT_HUB,
            BookingStatus.PICKED_UP,
            BookingStatus.IN_TRANSIT,
        ]

        if status_filter:
            if status_filter == "all":
                # Show all bookings except delivered, ordered by priority
                qs = (
                    Booking.objects.filter(driver=driver)
                    .exclude(status=BookingStatus.DELIVERED)
                    .select_related(
                        "pickup_address",
                        "dropoff_address",
                        "customer",
                        "driver",
                        "quote",
                    )
                    .prefetch_related("quote__shipping_type", "quote__service_type")
                )
                qs = qs.annotate(
                    status_priority=Case(
                        When(status=BookingStatus.AT_HUB, then=0),
                        When(status=BookingStatus.ASSIGNED, then=1),
                        When(status=BookingStatus.PICKED_UP, then=2),
                        When(status=BookingStatus.IN_TRANSIT, then=3),
                        default=4,
                        output_field=IntegerField(),
                    )
                ).order_by("status_priority", "-updated_at")
                paginator = self.pagination_class()
                paginator.page_size = page_size
                result_page = paginator.paginate_queryset(qs, request)
                serializer = BookingSerializer(result_page, many=True)
                return Response(
                    {
                        "ordered_bookings": serializer.data,
                        "count": paginator.page.paginator.count,
                        "is_optimized_route": False,
                        "route_id": None,
                        "hub_name": driver.hub.name if driver.hub else None,
                        "total_distance_km": 0,
                        "total_time_hours": 0,
                        "total_stops": paginator.page.paginator.count,
                        "next_stop_index": None,
                        "message": "Showing all bookings except delivered",
                    }
                )
            else:
                # Status-specific filter: all bookings with that status, ignoring route/manual distinction
                qs = (
                    Booking.objects.filter(driver=driver, status=status_filter)
                    .select_related(
                        "pickup_address",
                        "dropoff_address",
                        "customer",
                        "driver",
                        "quote",
                    )
                    .prefetch_related("quote__shipping_type", "quote__service_type")
                )
                qs = qs.annotate(
                    status_priority=Case(
                        When(status=BookingStatus.AT_HUB, then=0),
                        When(status=BookingStatus.ASSIGNED, then=1),
                        When(status=BookingStatus.PICKED_UP, then=2),
                        When(status=BookingStatus.IN_TRANSIT, then=3),
                        default=4,
                        output_field=IntegerField(),
                    )
                ).order_by("status_priority", "-updated_at")
                paginator = self.pagination_class()
                paginator.page_size = page_size
                result_page = paginator.paginate_queryset(qs, request)
                serializer = BookingSerializer(result_page, many=True)
                return Response(
                    {
                        "ordered_bookings": serializer.data,
                        "count": paginator.page.paginator.count,
                        "is_optimized_route": False,
                        "route_id": None,
                        "hub_name": driver.hub.name if driver.hub else None,
                        "total_distance_km": 0,
                        "total_time_hours": 0,
                        "total_stops": paginator.page.paginator.count,
                        "next_stop_index": None,
                        "message": f"Showing bookings with status: {status_filter}",
                    }
                )
        # No status filter: Unified current work (route + manuals)
        route = (
            Route.objects.filter(driver=driver, status__in=["assigned", "in_progress"])
            .select_related("hub", "hub__address")
            .order_by("-visible_at")
            .first()
        )
        route_bookings = []
        if route and route.ordered_stops:
            unfiltered_active = [
                b for b in route.ordered_bookings if b.status in active_statuses
            ]
            filtered_bookings = []
            for b in unfiltered_active:
                is_pickup = (
                    b.dropoff_address_id == route.hub.address_id
                    if route.hub and route.hub.address
                    else False
                )
                if b.status == BookingStatus.AT_HUB and is_pickup:
                    continue
                filtered_bookings.append(b)
            route_bookings = filtered_bookings
            # Check if all active bookings were hidden (all AT_HUB pickups) -> complete route
            if not filtered_bookings and unfiltered_active:
                route.status = "completed"
                route.save()
                route_bookings = []
                route = None  # Treat as no route now

        individual_qs = (
            Booking.objects.filter(
                driver=driver, route__isnull=True, status__in=active_statuses
            )
            .select_related(
                "pickup_address", "dropoff_address", "customer", "driver", "quote"
            )
            .prefetch_related("quote__shipping_type", "quote__service_type")
        )
        individual_qs = individual_qs.annotate(
            status_priority=Case(
                When(status=BookingStatus.AT_HUB, then=0),
                When(status=BookingStatus.ASSIGNED, then=1),
                When(status=BookingStatus.PICKED_UP, then=2),
                When(status=BookingStatus.IN_TRANSIT, then=3),
                default=4,
                output_field=IntegerField(),
            )
        ).order_by("status_priority", "-updated_at")
        individual_bookings = []
        for b in list(individual_qs):
            is_pickup = (
                b.dropoff_address_id == driver.hub.address_id
                if driver.hub and driver.hub.address
                else False
            )
            if b.status == BookingStatus.AT_HUB and is_pickup:
                continue
            individual_bookings.append(b)

        combined_bookings = route_bookings + individual_bookings
        total_count = len(combined_bookings)
        # Manual pagination (since mixed list + queryset)
        start = (page - 1) * page_size
        end = min(start + page_size, total_count)
        paged_bookings = combined_bookings[start:end]
        serializer = BookingSerializer(paged_bookings, many=True)
        # Calculate next_stop_index (on full route if exists)
        next_stop_index = None
        if route:
            for i, booking in enumerate(route_bookings):
                if booking.status in [
                    BookingStatus.AT_HUB,
                    BookingStatus.ASSIGNED,
                    BookingStatus.PICKED_UP,
                ]:
                    next_stop_index = i
                    break
        message = (
            "Active optimized route and additional manual assignments."
            if route and individual_bookings
            else (
                "Active optimized route." if route else "Individual manual assignments."
            )
        )
        return Response(
            {
                "ordered_bookings": serializer.data,
                "count": total_count,
                "is_optimized_route": bool(route),
                "route_id": str(route.id) if route else None,
                "hub_name": (
                    route.hub.name if route else driver.hub.name if driver.hub else None
                ),
                "total_distance_km": (
                    float(route.total_distance_km or 0) if route else 0
                ),
                "total_time_hours": float(route.total_time_hours or 0) if route else 0,
                "total_stops": total_count,
                "next_stop_index": next_stop_index,
                "message": message,
            }
        )


class DriverShiftViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = DriverShiftSerializer  # New serializer

    def get_queryset(self):
        return DriverShift.objects.filter(
            driver=self.request.user.driver_profile,
            start_time__date=timezone.now().date(),
        )

    @action(detail=True, methods=["post"], permission_classes=[IsAdminUser])
    def assign_driver(self, request, pk=None):
        shift = self.get_object()
        if shift.status != DriverShift.Status.PENDING:
            return Response({"error": "Shift not pending"}, status=400)

        driver_id = request.data.get("driver_id")
        try:
            driver = DriverProfile.objects.get(id=driver_id)
            if DriverShift.objects.filter(
                driver=driver,
                status__in=[DriverShift.Status.ASSIGNED, DriverShift.Status.ACTIVE],
            ).exists():
                return Response({"error": "Driver has open shift"}, status=400)

            shift.driver = driver
            shift.status = DriverShift.Status.ASSIGNED
            shift.save()

            # Update associated routes/bookings
            Route.objects.filter(shift=shift).update(driver=driver)
            Booking.objects.filter(route__shift=shift).update(driver=driver)

            return Response(DriverShiftSerializer(shift).data)
        except DriverProfile.DoesNotExist:
            return Response({"error": "Driver not found"}, status=404)


class DriverMetricsView(APIView):
    permission_classes = [IsDriver]

    @swagger_auto_schema(
        operation_description="Retrieve metrics for the authenticated driver's performance.",
        responses={
            200: openapi.Response(
                description="Driver metrics retrieved successfully",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        "total_deliveries": openapi.Schema(
                            type=openapi.TYPE_INTEGER,
                            description="Total number of successful deliveries",
                        ),
                        "failed_jobs": openapi.Schema(
                            type=openapi.TYPE_INTEGER,
                            description="Total number of failed deliveries",
                        ),
                        "active_jobs": openapi.Schema(
                            type=openapi.TYPE_INTEGER,
                            description="Total number of active jobs (assigned, picked up, or in transit)",
                        ),
                        "total_jobs": openapi.Schema(
                            type=openapi.TYPE_INTEGER,
                            description="Total number of assigned jobs",
                        ),
                        "completed_today": openapi.Schema(
                            type=openapi.TYPE_INTEGER,
                            description="Number of deliveries completed today",
                        ),
                        "completed_week": openapi.Schema(
                            type=openapi.TYPE_INTEGER,
                            description="Number of deliveries completed this week",
                        ),
                        "completed_month": openapi.Schema(
                            type=openapi.TYPE_INTEGER,
                            description="Number of deliveries completed this month",
                        ),
                        "completion_rate": openapi.Schema(
                            type=openapi.TYPE_NUMBER,
                            description="Percentage of successful deliveries out of successful plus failed deliveries",
                        ),
                        "average_rating": openapi.Schema(
                            type=openapi.TYPE_NUMBER,
                            description="Average rating of the driver",
                        ),
                    },
                    required=[
                        "total_deliveries",
                        "failed_jobs",
                        "active_jobs",
                        "total_jobs",
                        "completed_today",
                        "completed_week",
                        "completed_month",
                        "completion_rate",
                        "average_rating",
                    ],
                ),
            ),
            400: openapi.Response(
                description="Bad request, no driver profile found",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        "detail": openapi.Schema(
                            type=openapi.TYPE_STRING, description="Error message"
                        )
                    },
                ),
            ),
        },
    )
    def get(self, request):
        driver = getattr(request.user, "driver_profile", None)
        if not driver:
            return Response(
                {"detail": "No driver profile"}, status=status.HTTP_400_BAD_REQUEST
            )

        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timezone.timedelta(days=now.weekday())
        month_start = today_start.replace(day=1)

        bookings = Booking.objects.filter(driver=driver)
        delivered = bookings.filter(status=BookingStatus.DELIVERED)
        failed = bookings.filter(status=BookingStatus.FAILED)
        active_statuses = [
            BookingStatus.ASSIGNED,
            BookingStatus.PICKED_UP,
            BookingStatus.IN_TRANSIT,
        ]
        active = bookings.filter(status__in=active_statuses)

        total_deliveries = delivered.count()  # Successful jobs (delivered)
        failed_jobs = failed.count()
        total_jobs = bookings.count()  # All assigned bookings
        active_jobs = active.count()

        # Completion rate considering failed jobs: successful / (successful + failed)
        successful_plus_failed = total_deliveries + failed_jobs
        completion_rate = (
            round((total_deliveries / successful_plus_failed * 100), 2)
            if successful_plus_failed > 0
            else 0.0
        )

        # Average rating
        avg_rating = (
            DriverRating.objects.filter(driver_profile=driver).aggregate(
                avg=Avg("rating")
            )["avg"]
            or 0.0
        )

        data = {
            "total_deliveries": total_deliveries,
            "failed_jobs": failed_jobs,
            "active_jobs": active_jobs,
            "total_jobs": total_jobs,
            "completed_today": delivered.filter(updated_at__gte=today_start).count(),
            "completed_week": delivered.filter(updated_at__gte=week_start).count(),
            "completed_month": delivered.filter(updated_at__gte=month_start).count(),
            "completion_rate": completion_rate,
            "average_rating": round(avg_rating, 2),
        }

        return Response(data)


# New ViewSet for Driver Tracking


class DriverTrackingViewSet(
    viewsets.GenericViewSet, mixins.CreateModelMixin, mixins.ListModelMixin
):
    """
    Dedicated API for live driver tracking.
    - POST /driver/tracking/update/ → Driver sends current location
    - GET /driver/tracking/live/ → Admin gets all active drivers' latest locations
    - GET /driver/tracking/history/?driver_id=uuid&hours=1 → Get breadcrumb trail
    """

    queryset = DriverLocation.objects.select_related(
        "driver_profile__user", "driver_profile__hub"
    )
    serializer_class = DriverLocationSerializer
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        if self.action in ['live_locations', 'location_history']:
            return [IsAdmin()]  # Admins only for global views
        elif self.action == 'update_location':
            return [IsDriver()]  # Drivers update their location
        elif self.action == 'current_route':
            # Allow drivers for their own (no driver_id), admins for any
            if self.request.query_params.get('driver_id'):
                return [IsAdmin()]
            else:
                return [IsDriver()]
        return super().get_permissions()

    @action(detail=False, methods=["get"], url_path="current-route")
    def current_route(self, request):
        """
        Get the current route for a specific driver, including bookings (with lat/lng for stops).
        Query param: driver_id=uuid
        Used by frontend to compute directions.
        """
        driver_id = request.query_params.get("driver_id")
        if not driver_id:
            return Response({"error": "driver_id required"}, status=400)

        try:
            driver = DriverProfile.objects.get(id=driver_id)
            # Get the active route (assuming one active route per driver)
            route = (
                Route.objects.filter(
                    driver=driver, status__in=["assigned", "in_progress"]
                )
                .select_related("shift")
                .prefetch_related("bookings")
                .first()
            )

            if not route:
                return Response({"detail": "No active route found"}, status=404)

            serializer = RouteSerializer(
                route
            )  # Assuming RouteSerializer includes bookings with lat/lng
            return Response(serializer.data)
        except DriverProfile.DoesNotExist:
            return Response({"error": "Driver not found"}, status=404)

    @action(detail=False, methods=["post"], url_path="update-location")
    def update_location(self, request):
        try:
            driver_profile = request.user.driver_profile
        except AttributeError:
            return Response(
                {"error": "Driver profile not found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = DriverLocationCreateSerializer(data=request.data)
        if serializer.is_valid():
            location = DriverLocation.objects.create(
                driver_profile=driver_profile, **serializer.validated_data
            )
            # Update availability lat/lng if needed (from signals.py)
            return Response(
                DriverLocationSerializer(location).data, status=status.HTTP_201_CREATED
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="live")
    def live_locations(self, request):
        """
        Returns the LATEST location for each active driver.
        Query params:
            - hub_id=uuid
            - only_available=true
            - minutes_since_update=5 (only recent updates)
        """
        minutes = int(request.query_params.get("minutes_since_update", 10))
        cutoff = timezone.now() - timedelta(minutes=minutes)

        # Subquery: latest location per driver
        latest_locations = DriverLocation.objects.filter(
            driver_profile=OuterRef("driver_profile"), timestamp__gte=cutoff
        ).order_by("-timestamp")[:1]

        queryset = DriverLocation.objects.filter(
            id__in=Subquery(latest_locations.values("id"))
        ).select_related("driver_profile__user", "driver_profile__hub")

        # Filters
        hub_id = request.query_params.get("hub_id")
        if hub_id:
            queryset = queryset.filter(driver_profile__hub_id=hub_id)

        only_available = (
            request.query_params.get("only_available", "true").lower() == "true"
        )
        if only_available:
            queryset = queryset.filter(driver_profile__availability__available=True)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="history")
    def location_history(self, request):
        """
        Get breadcrumb trail for a driver.
        Required: driver_id=uuid
        Optional: hours=2, limit=100
        """
        driver_id = request.query_params.get("driver_id")
        if not driver_id:
            return Response({"error": "driver_id required"}, status=400)

        hours = int(request.query_params.get("hours", 4))
        limit = int(request.query_params.get("limit", 200))

        cutoff = timezone.now() - timedelta(hours=hours)

        queryset = (
            self.get_queryset()
            .filter(driver_profile_id=driver_id, timestamp__gte=cutoff)
            .order_by("timestamp")[:limit]
        )

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
