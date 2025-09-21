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
from django.db.models import Avg
from rest_framework.permissions import IsAdminUser
from django.db import transaction
from rest_framework.parsers import MultiPartParser, FormParser

from bookings.models import Booking, BookingStatus
from bookings.serializers import BookingSerializer
from driver.models import (
    DriverAvailability, DriverPayout, DriverRating, DriverDocument,
)
from driver.serializers import (
    DriverAvailabilitySerializer,
    DriverPayoutSerializer, DriverPayoutCreateSerializer,
    DriverRatingSerializer, DriverDocumentSerializer, DriverInviteCreateSerializer, DriverInviteDetailSerializer,
    DriverInviteAcceptSerializer,
)
from users.serializers import UserSerializer
from .permissions import IsAdmin, IsDriver, IsCustomer


class DriverAvailabilityViewSet(mixins.CreateModelMixin,
                                mixins.ListModelMixin,
                                viewsets.GenericViewSet):
    serializer_class = DriverAvailabilitySerializer

    def get_permissions(self):
        if self.action in ["list"]:
            return [IsDriver()]
        if self.action in ["create", "list", "update", "partial_update", "destroy"]:
            return [IsAdmin()]
        return super().get_permissions()

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return DriverAvailability.objects.none()
        return DriverAvailability.objects.select_related("driver_profile", "driver_profile__user")

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
        if getattr(self, 'swagger_fake_view', False):
            return DriverPayout.objects.none()
        u = self.request.user
        qs = DriverPayout.objects.select_related(
            "driver_profile", "driver_profile__user")
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


class DriverRatingViewSet(mixins.CreateModelMixin,
                          mixins.ListModelMixin,
                          viewsets.GenericViewSet):
    serializer_class = DriverRatingSerializer

    def get_permissions(self):
        if self.action == "create":
            return [IsCustomer()]
        if self.action == "list":
            return []
        return super().get_permissions()

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
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
        new_avg = (profile.rating_avg * profile.rating_count +
                   Decimal(rating.rating)) / Decimal(new_count)
        profile.rating_count = new_count
        profile.rating_avg = new_avg.quantize(Decimal("0.01"))
        profile.save(update_fields=["rating_count", "rating_avg"])
        return Response(DriverRatingSerializer(rating).data, status=201)


class DriverDocumentViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet
):
    serializer_class = DriverDocumentSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def get_permissions(self):
        if self.action == "verify_document":
            return [IsAdminUser()]
        return [IsAuthenticated(), IsDriver()]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return DriverDocument.objects.none()
        user = self.request.user
        if user.role == 'admin':
            return DriverDocument.objects.all()
        if hasattr(user, "driver_profile"):
            return user.driver_profile.documents.all()
        return DriverDocument.objects.none()

    @transaction.atomic
    def perform_create(self, serializer):
        if not hasattr(self.request.user, 'driver_profile'):
            print("User has no driver profile:", self.request.user)
            raise serializers.ValidationError(
                {"detail": "User must have a driver profile to upload documents."}
            )
        driver = self.request.user.driver_profile
        doc_type = serializer.validated_data.get('doc_type')
        print("Uploading document of type:", doc_type)
        print("Driver ID:", driver.id)
        print("File info:", serializer.validated_data.get('file'))
        # Check if document already exists for this driver and doc_type
        existing_doc = DriverDocument.objects.filter(
            driver=driver, doc_type=doc_type
        ).first()
        if existing_doc:
            # Update existing document
            existing_doc.file = serializer.validated_data['file']
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
                {"detail": f"Document is already {'verified' if verified else 'unverified'}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        document.verified = verified
        document.notes = notes if notes is not None else document.notes
        document.save()

        return Response(DriverDocumentSerializer(document).data)


class DriverInviteViewSet(mixins.CreateModelMixin,
                          mixins.ListModelMixin,
                          viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get_queryset(self):
        from .models import DriverInvitation
        return DriverInvitation.objects.all().order_by("-expires_at")

    def get_serializer_class(self):
        if self.action == "create":
            return DriverInviteCreateSerializer
        return DriverInviteDetailSerializer

    @action(methods=["post"], detail=False, url_path="accept", permission_classes=[AllowAny])
    def accept(self, request):
        s = DriverInviteAcceptSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        user = s.save()
        return Response(UserSerializer(user).data, status=201)


class StandardPagination(PageNumberPagination):
    # Default items per page (adjust based on UI needs, e.g., 20-50)
    page_size = 10
    # Allow frontend to override, e.g., ?page_size=20
    page_size_query_param = 'page_size'
    max_page_size = 100  # Prevent abuse


class DriverAssignedBookingViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = BookingSerializer
    permission_classes = [IsAuthenticated, IsDriver | IsAdmin]
    pagination_class = StandardPagination

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Booking.objects.none()

        user = self.request.user
        driver_id = self.request.query_params.get("driver_id")
        status_param = self.request.query_params.get("status")

        active_statuses = [
            BookingStatus.ASSIGNED,
            BookingStatus.PICKED_UP,
            BookingStatus.IN_TRANSIT,
            BookingStatus.DELIVERED
        ]

        # Base queryset
        queryset = Booking.objects.select_related(
            "pickup_address", "dropoff_address", "customer", "driver", "quote"
        ).prefetch_related(
            "quote__shipping_type", "quote__service_type"
        ).filter(status__in=active_statuses).order_by("-created_at")

        # Apply status filter if provided
        if status_param and status_param in BookingStatus.values:
            queryset = queryset.filter(status=status_param)
        else:
            queryset = queryset.filter(status__in=active_statuses)

        # If driver_id is provided and user is admin
        if driver_id and hasattr(user, "role") and user.role == "admin":
            try:
                # Validate driver_id as UUID
                uuid.UUID(driver_id)
                queryset = queryset.filter(driver_id=driver_id)
            except ValueError:
                return Booking.objects.none()  # Invalid driver_id
        # For non-admin drivers, restrict to their own bookings
        elif hasattr(user, "driver_profile"):
            queryset = queryset.filter(driver=user.driver_profile)
        else:
            return Booking.objects.none()  # No driver profile or not admin

        return queryset

    def list(self, request, *args, **kwargs):
        # Rely on DRF's authentication and permission classes
        return super().list(request, *args, **kwargs)

# class DriverAssignedBookingViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
#     serializer_class = BookingSerializer
#     permission_classes = [IsAuthenticated, IsDriver | IsAdmin]
#
#     def get_queryset(self):
#         if getattr(self, 'swagger_fake_view', False):
#             return Booking.objects.none()
#         user = self.request.user
#         driver_id = self.request.query_params.get("driver_id")
#
#         # Base queryset
#         queryset = Booking.objects.select_related(
#             "pickup_address", "dropoff_address", "customer", "driver", "quote"
#         ).prefetch_related(
#             "quote__shipping_type", "quote__service_type"
#         ).filter(status=BookingStatus.ASSIGNED).order_by("-created_at")
#
#         # If driver_id is provided and user is admin
#         if driver_id and hasattr(user, "role") and user.role == "admin":
#             try:
#                 # Validate driver_id as UUID
#                 uuid.UUID(driver_id)
#                 queryset = queryset.filter(driver_id=driver_id)
#             except ValueError:
#                 return Booking.objects.none()  # Invalid driver_id
#         # For non-admin drivers, restrict to their own bookings
#         elif hasattr(user, "driver_profile"):
#             queryset = queryset.filter(driver=user.driver_profile)
#         else:
#             return Booking.objects.none()  # No driver profile
#
#         return queryset
#
#     def list(self, request, *args, **kwargs):
#         # Validate token if provided in Authorization header
#         auth_token = request.META.get("HTTP_AUTHORIZATION", "").split("Token ")[1] if "Token " in request.META.get(
#             "HTTP_AUTHORIZATION", "") else None
#         if auth_token:
#             try:
#                 token = Token.objects.get(key=auth_token)
#                 user = token.user
#                 if not hasattr(user, "driver_profile") and user.role != "admin":
#                     return Response({"detail": "Invalid token: User is not a driver or admin"},
#                                     status=status.HTTP_403_FORBIDDEN)
#             except Token.DoesNotExist:
#                 return Response({"detail": "Invalid token"}, status=status.HTTP_401_UNAUTHORIZED)
#         return super().list(request, *args, **kwargs)


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
                        "total_deliveries": openapi.Schema(type=openapi.TYPE_INTEGER, description="Total number of successful deliveries"),
                        "failed_jobs": openapi.Schema(type=openapi.TYPE_INTEGER, description="Total number of failed deliveries"),
                        "active_jobs": openapi.Schema(type=openapi.TYPE_INTEGER, description="Total number of active jobs (assigned, picked up, or in transit)"),
                        "total_jobs": openapi.Schema(type=openapi.TYPE_INTEGER, description="Total number of assigned jobs"),
                        "completed_today": openapi.Schema(type=openapi.TYPE_INTEGER, description="Number of deliveries completed today"),
                        "completed_week": openapi.Schema(type=openapi.TYPE_INTEGER, description="Number of deliveries completed this week"),
                        "completed_month": openapi.Schema(type=openapi.TYPE_INTEGER, description="Number of deliveries completed this month"),
                        "completion_rate": openapi.Schema(type=openapi.TYPE_NUMBER, description="Percentage of successful deliveries out of successful plus failed deliveries"),
                        "average_rating": openapi.Schema(type=openapi.TYPE_NUMBER, description="Average rating of the driver"),
                    },
                    required=["total_deliveries", "failed_jobs", "active_jobs", "total_jobs", "completed_today",
                              "completed_week", "completed_month", "completion_rate", "average_rating"]
                )
            ),
            400: openapi.Response(
                description="Bad request, no driver profile found",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        "detail": openapi.Schema(type=openapi.TYPE_STRING, description="Error message")
                    }
                )
            )
        },
    )
    def get(self, request):
        driver = getattr(request.user, "driver_profile", None)
        if not driver:
            return Response({"detail": "No driver profile"}, status=status.HTTP_400_BAD_REQUEST)

        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timezone.timedelta(days=now.weekday())
        month_start = today_start.replace(day=1)

        bookings = Booking.objects.filter(driver=driver)
        delivered = bookings.filter(status=BookingStatus.DELIVERED)
        failed = bookings.filter(status=BookingStatus.FAILED)
        active_statuses = [BookingStatus.ASSIGNED,
                           BookingStatus.PICKED_UP, BookingStatus.IN_TRANSIT]
        active = bookings.filter(status__in=active_statuses)

        total_deliveries = delivered.count()  # Successful jobs (delivered)
        failed_jobs = failed.count()
        total_jobs = bookings.count()  # All assigned bookings
        active_jobs = active.count()

        # Completion rate considering failed jobs: successful / (successful + failed)
        successful_plus_failed = total_deliveries + failed_jobs
        completion_rate = round(
            (total_deliveries / successful_plus_failed * 100), 2) if successful_plus_failed > 0 else 0.0

        # Average rating
        avg_rating = DriverRating.objects.filter(
            driver_profile=driver).aggregate(avg=Avg("rating"))["avg"] or 0.0

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
