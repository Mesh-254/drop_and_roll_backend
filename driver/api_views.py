import uuid
from decimal import Decimal

from django.utils import timezone
from rest_framework import viewsets, mixins
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from rest_framework import status

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
        if self.action in ["create"]:
            return [IsDriver()]
        if self.action in ["list"]:
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
        qs = DriverPayout.objects.select_related("driver_profile", "driver_profile__user")
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
        new_avg = (profile.rating_avg * profile.rating_count + Decimal(rating.rating)) / Decimal(new_count)
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
    permission_classes = [IsAuthenticated, IsDriver]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return DriverDocument.objects.none()
        user = self.request.user
        if hasattr(user, "driver_profile"):
            return user.driver_profile.documents.all()
        return DriverDocument.objects.none()


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


class DriverAssignedBookingViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = BookingSerializer
    permission_classes = [IsAuthenticated, IsDriver | IsAdmin]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Booking.objects.none()
        user = self.request.user
        driver_id = self.request.query_params.get("driver_id")

        # Base queryset
        queryset = Booking.objects.select_related(
            "pickup_address", "dropoff_address", "customer", "driver", "quote"
        ).prefetch_related(
            "quote__shipping_type", "quote__service_type"
        ).filter(status=BookingStatus.ASSIGNED).order_by("-created_at")

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
            return Booking.objects.none()  # No driver profile

        return queryset

    def list(self, request, *args, **kwargs):
        # Validate token if provided in Authorization header
        auth_token = request.META.get("HTTP_AUTHORIZATION", "").split("Token ")[1] if "Token " in request.META.get(
            "HTTP_AUTHORIZATION", "") else None
        if auth_token:
            try:
                token = Token.objects.get(key=auth_token)
                user = token.user
                if not hasattr(user, "driver_profile") and user.role != "admin":
                    return Response({"detail": "Invalid token: User is not a driver or admin"},
                                    status=status.HTTP_403_FORBIDDEN)
            except Token.DoesNotExist:
                return Response({"detail": "Invalid token"}, status=status.HTTP_401_UNAUTHORIZED)
        return super().list(request, *args, **kwargs)