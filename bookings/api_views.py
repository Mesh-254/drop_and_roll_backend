from decimal import Decimal

from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, IsAuthenticatedOrReadOnly
from rest_framework.response import Response

from .models import Quote, Booking, RecurringSchedule, BookingStatus, ShippingType, ServiceType
from .permissions import IsCustomer, IsAdminOrReadOnly
from .serializers import (
    QuoteRequestSerializer,
    QuoteSerializer,
    BookingCreateSerializer,
    BookingSerializer,
    RecurringScheduleSerializer, ShippingTypeSerializer, ServiceTypeSerializer,
)
from .utils.pricing import compute_quote


class QuoteViewSet(viewsets.GenericViewSet):
    queryset = Quote.objects.all()

    @swagger_auto_schema(
        method="post",
        request_body=QuoteRequestSerializer,
        responses={201: QuoteSerializer}
    )
    @action(methods=["post"], detail=False, url_path="compute")
    def compute(self, request):
        serializer = QuoteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        price, final, breakdown = compute_quote(
            service_tier=data["service_tier"],
            weight_kg=Decimal(data["weight_kg"]),
            distance_km=Decimal(data["distance_km"]),
            surge=Decimal(data["surge"]),
            discount=Decimal(data["discount"]),
        )

        quote = Quote.objects.create(
            service_tier=data["service_tier"],
            weight_kg=data["weight_kg"],
            distance_km=data["distance_km"],
            base_price=price,
            surge_multiplier=data["surge"],
            discount_amount=data["discount"],
            final_price=final,
            meta=breakdown,
        )
        return Response(QuoteSerializer(quote).data, status=status.HTTP_201_CREATED)


class BookingViewSet(viewsets.ModelViewSet):
    queryset = Booking.objects.select_related("pickup_address", "dropoff_address", "customer", "driver", "quote")
    serializer_class = BookingSerializer

    def get_permissions(self):
        if self.action in ["create", "bulk_upload", "recurring_list", "recurring_create"]:
            return [IsCustomer()]
        if self.action in ["update", "partial_update", "destroy", "assign_driver", "set_status"]:
            return [IsAdminOrReadOnly()]
        if self.action in ["retrieve", "list"]:
            return []
        return super().get_permissions()

    def get_serializer_class(self):
        if self.action == "create":
            return BookingCreateSerializer
        return BookingSerializer

    def perform_create(self, serializer):
        serializer.save()

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if not user.is_authenticated:
            return qs.none()
        role = getattr(user, "role", None)
        if role == "customer":
            return qs.filter(customer=user)
        if role == "driver":
            return qs.filter(driver__user=user)
        return qs

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
    def recurring_create(self, request):
        serializer = RecurringScheduleSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        obj = serializer.save()
        return Response(RecurringScheduleSerializer(obj).data, status=201)


class ShippingTypeViewSet(viewsets.ModelViewSet):
    queryset = ShippingType.objects.all()
    serializer_class = ShippingTypeSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

class ServiceTypeViewSet(viewsets.ModelViewSet):
    queryset = ServiceType.objects.all()
    serializer_class = ServiceTypeSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
