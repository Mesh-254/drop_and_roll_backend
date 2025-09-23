# business/views.py
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from django.db.transaction import atomic
from .models import BusinessInquiry, BusinessPricing, BusinessInquiryStatus
from .serializers import BusinessInquirySerializer, BusinessPricingSerializer
from users.permissions import IsAdmin  # Reusing from users app (adjust if needed)
from bookings.models import Quote, Booking, BookingStatus, Address, ShippingType, ServiceType
from payments.models import PaymentTransaction, PaymentStatus
from .utils.pricing import compute_business_quote
import uuid
import shortuuid
from django.utils import timezone


class BusinessPricingViewSet(viewsets.ModelViewSet):
    queryset = BusinessPricing.objects.all().order_by('-created_at')
    serializer_class = BusinessPricingSerializer
    permission_classes = [IsAdmin]


class BusinessInquiryViewSet(viewsets.ModelViewSet):
    queryset = BusinessInquiry.objects.all().order_by('-created_at')
    serializer_class = BusinessInquirySerializer
    permission_classes = [AllowAny]

    def get_permissions(self):
        if self.action in ['create']:
            return [AllowAny()]
        if self.action in ['list', 'retrieve', 'update', 'partial_update', 'destroy', 'generate_quote',
                           'create_booking', 'approve_booking', 'assign_driver']:
            return [IsAdmin()]
        return super().get_permissions()

    def get_queryset(self):
        qs = super().get_queryset()
        if not self.request.user.is_authenticated or getattr(self.request.user, 'role', None) != 'admin':
            return qs.none()
        return qs

    @atomic
    @action(methods=['post'], detail=True, url_path='generate-quote')
    def generate_quote(self, request, pk=None):
        inquiry = self.get_object()
        if inquiry.status != BusinessInquiryStatus.PENDING:
            return Response({"detail": "Quote can only be generated for pending inquiries"},
                            status=status.HTTP_400_BAD_REQUEST)

        data = request.data or {}
        shipping_type = inquiry.shipping_type or ShippingType.objects.get(id=data.get('shipping_type_id'))
        service_type = inquiry.service_type or ServiceType.objects.get(id=data.get('service_type_id'))
        weight_kg = inquiry.weight_kg or data.get('weight_kg', 0)
        distance_km = inquiry.distance_km or data.get('distance_km', 0)
        fragile = inquiry.fragile or data.get('fragile', False)
        insurance_amount = inquiry.insurance_amount or data.get('insurance_amount', 0)
        dimensions = inquiry.dimensions or data.get('dimensions', {})
        surge = data.get('surge_multiplier', 1)
        discount = data.get('discount_amount', 0)

        base_price, final_price, breakdown = compute_business_quote(
            shipping_type=shipping_type,
            service_type=service_type,
            weight_kg=weight_kg,
            distance_km=distance_km,
            fragile=fragile,
            insurance_amount=insurance_amount,
            dimensions=dimensions,
            surge=surge,
            discount=discount,
        )

        quote = Quote.objects.create(
            shipping_type=shipping_type,
            service_type=service_type,
            weight_kg=weight_kg,
            distance_km=distance_km,
            fragile=fragile,
            insurance_amount=insurance_amount,
            dimensions=dimensions,
            base_price=base_price,
            surge_multiplier=surge,
            discount_amount=discount,
            final_price=final_price,
            meta=breakdown,
        )

        inquiry.quote = quote
        inquiry.status = BusinessInquiryStatus.QUOTED
        inquiry.save()

        return Response(BusinessInquirySerializer(inquiry).data)

    @atomic
    @action(methods=['post'], detail=True, url_path='create-booking')
    def create_booking(self, request, pk=None):
        inquiry = self.get_object()
        if inquiry.status != BusinessInquiryStatus.QUOTED or not inquiry.quote:
            return Response({"detail": "Booking can only be created for quoted inquiries"},
                            status=status.HTTP_400_BAD_REQUEST)

        data = request.data or {}
        pickup_address = inquiry.pickup_address or Address.objects.get(id=data.get('pickup_address_id')) if data.get(
            'pickup_address_id') else None
        dropoff_address = inquiry.dropoff_address or Address.objects.get(id=data.get('dropoff_address_id')) if data.get(
            'dropoff_address_id') else None
        scheduled_pickup_at = data.get('scheduled_pickup_at')
        notes = data.get('notes', inquiry.description)

        booking = Booking.objects.create(
            customer=inquiry.user if inquiry.user and inquiry.user.role == 'customer' else None,
            guest_email=inquiry.email if not inquiry.user else None,
            quote=inquiry.quote,
            final_price=inquiry.quote.final_price,
            pickup_address=pickup_address,
            dropoff_address=dropoff_address,
            status=BookingStatus.PENDING,
            scheduled_pickup_at=scheduled_pickup_at,
            notes=notes,
            payment_expires_at=timezone.now() + timezone.timedelta(days=1),
        )

        inquiry.booking = booking
        inquiry.status = BusinessInquiryStatus.BOOKED
        inquiry.save()

        if booking.final_price > 0:
            tx = PaymentTransaction.objects.create(
                user=booking.customer,
                guest_email=booking.guest_email,
                booking=booking,
                amount=booking.final_price,
                status=PaymentStatus.PENDING,
                reference=str(uuid.uuid4())[:12].replace("-", "")
            )

        return Response(BusinessInquirySerializer(inquiry).data)

    @action(methods=['post'], detail=True, url_path='approve-booking')
    def approve_booking(self, request, pk=None):
        inquiry = self.get_object()
        if inquiry.status != BusinessInquiryStatus.BOOKED or not inquiry.booking:
            return Response({"detail": "Only booked inquiries can be approved"}, status=status.HTTP_400_BAD_REQUEST)

        booking = inquiry.booking
        booking.status = BookingStatus.SCHEDULED
        booking.tracking_number = f"BK-{shortuuid.uuid()[:6].upper()}"
        booking.save()

        return Response(BookingSerializer(booking).data)

    @action(methods=['post'], detail=True, url_path='assign-driver')
    def assign_driver(self, request, pk=None):
        inquiry = self.get_object()
        if inquiry.status != BusinessInquiryStatus.BOOKED or not inquiry.booking:
            return Response({"detail": "Only booked inquiries can have drivers assigned"},
                            status=status.HTTP_400_BAD_REQUEST)

        driver_id = request.data.get('driver_profile_id')
        if not driver_id:
            return Response({"detail": "driver_profile_id required"}, status=status.HTTP_400_BAD_REQUEST)

        booking = inquiry.booking
        booking.driver_id = driver_id
        booking.status = BookingStatus.ASSIGNED
        booking.save()

        return Response(BookingSerializer(booking).data)