from rest_framework import viewsets, mixins, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from django.shortcuts import get_object_or_404
from django.utils import timezone
import csv, io

from .models import Address, Quote, Booking, RecurringSchedule, BulkUpload, BookingStatus
from .serializers import (
    AddressSerializer,
    QuoteRequestSerializer,
    QuoteSerializer,
    BookingCreateSerializer,
    BookingSerializer,
    RecurringScheduleSerializer,
    BulkUploadSerializer,
)
from .permissions import IsCustomer, IsAdminOrReadOnly, IsOwnerOrAdmin
from .utils.pricing import compute_quote
from decimal import Decimal


class QuoteViewSet(viewsets.GenericViewSet):
    queryset = Quote.objects.all()

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
            return []  # default DRF permissions (can be overridden globally)
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
        return qs  # admin/staff sees all

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

    @action(methods=["post"], detail=True, url_path="set-status")
    def set_status(self, request, pk=None):
        booking = self.get_object()
        status_value = request.data.get("status")
        if status_value not in BookingStatus.values:
            return Response({"detail": "Invalid status"}, status=400)
        booking.status = status_value
        booking.save(update_fields=["status", "updated_at"])
        return Response({"id": str(booking.id), "status": booking.status})

    @action(methods=["post"], detail=False, url_path="bulk-upload", parser_classes=[MultiPartParser, FormParser])
    def bulk_upload(self, request):
        user = request.user
        upload = BulkUpload.objects.create(customer=user, csv_file=request.data.get("file"))
        # Parse immediately for MVP (could be handed off to Celery in production)
        f = upload.csv_file.open("rb")
        content = f.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(content))
        created, errors = 0, []
        for idx, row in enumerate(reader, start=1):
            try:
                # Required columns: pickup_line1, pickup_city, drop_line1, drop_city, weight_kg, distance_km, service_tier, quote_id
                pickup = Address.objects.create(line1=row["pickup_line1"], city=row["pickup_city"], region=row.get("pickup_region"), postal_code=row.get("pickup_postal"), country=row.get("pickup_country", "KE"))
                drop = Address.objects.create(line1=row["drop_line1"], city=row["drop_city"], region=row.get("drop_region"), postal_code=row.get("drop_postal"), country=row.get("drop_country", "KE"))
                quote = Quote.objects.get(pk=row["quote_id"])
                Booking.objects.create(
                    customer=user,
                    pickup_address=pickup,
                    dropoff_address=drop,
                    service_tier=row["service_tier"],
                    status=BookingStatus.SCHEDULED,
                    weight_kg=row["weight_kg"],
                    distance_km=row["distance_km"],
                    quote=quote,
                    final_price=quote.final_price,
                )
                created += 1
            except Exception as e:
                errors.append({"row": idx, "error": str(e)})
        upload.processed = True
        upload.processed_at = timezone.now()
        upload.result = {"created": created, "errors": errors}
        upload.save(update_fields=["processed", "processed_at", "result"])
        return Response(BulkUploadSerializer(upload).data)

    # Recurring APIs (simple grouping here)
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