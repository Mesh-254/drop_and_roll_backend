from decimal import Decimal

from django.utils import timezone
from drf_yasg.utils import swagger_auto_schema
from rest_framework import viewsets, mixins
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response

from driver.models import (
    DriverProfile, DriverDocument, DriverAvailability, DriverPayout, DriverRating, DriverInvite,
    DocumentStatus, DriverStatus
)
from driver.serializers import (
    DriverProfileSerializer, DriverProfileUpdateSerializer,
    DriverDocumentCreateSerializer, DriverDocumentReviewSerializer,
    DriverAvailabilitySerializer,
    DriverPayoutSerializer, DriverPayoutCreateSerializer,
    DriverRatingSerializer,
    DriverInviteSerializer, DriverInviteAcceptSerializer,
)
from users.serializers import DriverDocumentSerializer
from .permissions import IsAdmin, IsDriver, IsCustomer


class DriverProfileViewSet(viewsets.ModelViewSet):
    queryset = DriverProfile.objects.select_related("user")
    serializer_class = DriverProfileSerializer

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return DriverProfile.objects.none()
        u = self.request.user
        qs = super().get_queryset()
        if not u.is_authenticated:
            return qs.none()
        role = getattr(u, "role", None)
        if role == "driver":
            return qs.filter(user=u)
        if role == "customer":
            # customers can read drivers, but not list all (restrict via query params or business rules)
            driver_id = self.request.query_params.get("id")
            if driver_id:
                return qs.filter(id=driver_id)
            return qs.none()
        # admin can see all
        return qs

    def get_permissions(self):
        if self.action in ["list", "retrieve"]:
            return []  # allow per get_queryset scoping
        if self.action in ["update", "partial_update"]:
            return [IsDriver()]
        if self.action in ["destroy", "create"]:
            return [IsAdmin()]
        return super().get_permissions()

    def get_serializer_class(self):
        if self.action in ["update", "partial_update"]:
            return DriverProfileUpdateSerializer
        return DriverProfileSerializer

    # @swagger_auto_schema(method="get", responses={200: DriverProfileSerializer})
    # @action(methods=["get"], detail=False, url_path="me")
    # def me(self, request):
    #     if not hasattr(request.user, "driver_profile"):
    #         return Response({"detail": "No driver profile"}, status=404)
    #     return Response(DriverProfileSerializer(request.user.driver_profile).data)


class DriverDocumentViewSet(mixins.CreateModelMixin,
                            mixins.ListModelMixin,
                            mixins.DestroyModelMixin,
                            viewsets.GenericViewSet):
    serializer_class = DriverDocumentSerializer
    parser_classes = [MultiPartParser, FormParser]

    def get_permissions(self):
        if self.action in ["create", "list", "destroy"]:
            return [IsDriver()]
        return super().get_permissions()

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return DriverDocument.objects.none()
        return DriverDocument.objects.filter(driver_profile__user=self.request.user)

    def get_serializer_class(self):
        if self.action == "create":
            return DriverDocumentCreateSerializer
        return DriverDocumentSerializer

    # @swagger_auto_schema(method="post", request_body=DriverDocumentCreateSerializer, responses={201: DriverDocumentSerializer})
    def create(self, request, *args, **kwargs):
        profile = getattr(request.user, "driver_profile", None)
        if profile is None:
            return Response({"detail": "No driver profile"}, status=400)
        s = DriverDocumentCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        doc = DriverDocument.objects.create(driver_profile=profile, **s.validated_data)
        return Response(DriverDocumentSerializer(doc).data, status=201)

    # Admin moderation as separate endpoint (not part of driver's own set)


class DriverDocumentModerationViewSet(mixins.ListModelMixin,
                                      mixins.UpdateModelMixin,
                                      viewsets.GenericViewSet):
    queryset = DriverDocument.objects.select_related("driver_profile", "driver_profile__user")
    serializer_class = DriverDocumentSerializer

    def get_permissions(self):
        return [IsAdmin()]

    @swagger_auto_schema(method="patch", request_body=DriverDocumentReviewSerializer,
                         responses={200: DriverDocumentSerializer})
    @action(methods=["patch"], detail=True, url_path="review")
    def review(self, request, pk=None):
        doc = self.get_object()
        s = DriverDocumentReviewSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        doc.status = s.validated_data["status"]
        doc.reason = s.validated_data.get("reason", "")
        doc.reviewed_at = timezone.now()
        doc.save(update_fields=["status", "reason", "reviewed_at"])
        # If approved, verify driver
        if doc.status == DocumentStatus.APPROVED:
            DriverProfile.objects.filter(id=doc.driver_profile_id).update(is_verified=True)
        return Response(DriverDocumentSerializer(doc).data)


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
        # Optionally update profile status
        if obj.available and profile.status != DriverStatus.ACTIVE:
            profile.status = DriverStatus.ACTIVE
            profile.save(update_fields=["status"])
        if not obj.available and profile.status == DriverStatus.ACTIVE:
            profile.status = DriverStatus.INACTIVE
            profile.save(update_fields=["status"])
        return Response(DriverAvailabilitySerializer(obj).data)


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


class DriverInviteViewSet(mixins.CreateModelMixin,
                          mixins.ListModelMixin,
                          viewsets.GenericViewSet):
    serializer_class = DriverInviteSerializer

    def get_permissions(self):
        if self.action in ["create", "list"]:
            return [IsAdmin()]
        return super().get_permissions()

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return DriverInvite.objects.none()
        return DriverInvite.objects.all()

    # @swagger_auto_schema(method="post", request_body=DriverInviteSerializer, responses={201: DriverInviteSerializer})
    def create(self, request, *args, **kwargs):
        s = DriverInviteSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        invite = s.save(invited_by=request.user)
        # TODO: send email with invite.token
        return Response(DriverInviteSerializer(invite).data, status=201)

    # @swagger_auto_schema(method="post", request_body=DriverInviteAcceptSerializer, responses={200: DriverProfileSerializer})
    @action(methods=["post"], detail=False, url_path="accept", permission_classes=[])  # AllowAny
    def accept(self, request):
        s = DriverInviteAcceptSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        profile = s.save()
        return Response(DriverProfileSerializer(profile).data)
