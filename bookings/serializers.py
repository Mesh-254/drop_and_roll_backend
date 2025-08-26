import uuid
from rest_framework import serializers
from .models import Address, Quote, Booking, RecurringSchedule, BulkUpload, ServiceType, BookingStatus
from .utils.pricing import compute_quote
from decimal import Decimal

from rest_framework import serializers

from bookings.models import Address, Quote, Booking, RecurringSchedule, BulkUpload, ServiceType, ShippingType, \
    ServiceType


class ShippingTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShippingType
        read_only_fields = ["id", "created_at", "updated_at"]
        fields = ["id", "name", "description"]


class ServiceTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceType
        fields = ["id", "name", "description", "price", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class AddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = Address
        fields = ["id", "line1", "line2", "city", "region", "postal_code", "country", "latitude", "longitude",
                  "validated"]
        read_only_fields = ["id", "validated"]


class QuoteRequestSerializer(serializers.Serializer):
    shipping_type_id = serializers.UUIDField()
    service_type_id = serializers.UUIDField()

    weight_kg = serializers.DecimalField(
        max_digits=6, decimal_places=2, min_value=Decimal("0"))
    distance_km = serializers.DecimalField(
        max_digits=7, decimal_places=2, min_value=Decimal("0"))

    surge = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, default=Decimal("1.00"))
    discount = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, default=Decimal("0.00"))

    fragile = serializers.BooleanField(default=False)
    insurance_amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, default=Decimal("0.00"))
    dimensions = serializers.JSONField(required=False, default=dict)

    def validate_shipping_type_id(self, value):
        if not ShippingType.objects.filter(id=value).exists():
            raise serializers.ValidationError("Invalid shipping type ID")
        return value

    def validate_service_type_id(self, value):
        if not ServiceType.objects.filter(id=value).exists():
            raise serializers.ValidationError("Invalid service type ID")
        return value

class FloatDecimalField(serializers.DecimalField):
    def to_representation(self, value):
        if value is None:
            return None
        return float(value)
    
class QuoteSerializer(serializers.ModelSerializer):
    # Nested serializers for read
    shipping_type = ShippingTypeSerializer(read_only=True)
    service_type = ServiceTypeSerializer(read_only=True)

    # Write-only IDs for POST/PUT
    shipping_type_id = serializers.UUIDField(
        write_only=True, required=False, allow_null=True)
    service_type_id = serializers.UUIDField(
        write_only=True, required=False, allow_null=True)
    
    # Use FloatDecimalField for all Decimal fields
    final_price = FloatDecimalField(max_digits=10, decimal_places=2)
    base_price = FloatDecimalField(max_digits=10, decimal_places=2)
    surge_multiplier = FloatDecimalField(max_digits=5, decimal_places=2)
    discount_amount = FloatDecimalField(max_digits=10, decimal_places=2)
    weight_kg = FloatDecimalField(max_digits=6, decimal_places=2)
    distance_km = FloatDecimalField(max_digits=7, decimal_places=2)
    insurance_amount = FloatDecimalField(max_digits=10, decimal_places=2)

    class Meta:
        model = Quote
        fields = [
            "id",
            "created_at",
            "weight_kg",
            "distance_km",
            "fragile",
            "insurance_amount",
            "dimensions",
            "base_price",
            "surge_multiplier",
            "discount_amount",
            "final_price",
            "shipping_type",
            "shipping_type_id",
            "service_type",
            "service_type_id",
            "meta",
        ]
        read_only_fields = ["id", "created_at", "final_price", "base_price"]

    def create(self, validated_data):
        shipping_type_id = validated_data.pop("shipping_type_id")
        service_type_id = validated_data.pop("service_type_id")

        shipping_type = ShippingType.objects.get(id=shipping_type_id)
        service_type = ServiceType.objects.get(id=service_type_id)

        base_price, final_price, meta = compute_quote(
            shipment_type=shipping_type.name,
            service_type=service_type.name,
            weight_kg=validated_data["weight_kg"],
            distance_km=validated_data["distance_km"],
            fragile=validated_data["fragile"],
            insurance_amount=validated_data["insurance_amount"],
            dimensions=validated_data["dimensions"],
            surge=validated_data["surge"],
            discount=validated_data["discount"],
        )

        validated_data["base_price"] = base_price
        validated_data["meta"] = meta
        validated_data["final_price"] = final_price

        quote = Quote.objects.create(
            shipping_type=shipping_type,
            service_type=service_type,
            **validated_data
        )
        return quote


class BookingCreateSerializer(serializers.ModelSerializer):
    pickup_address = AddressSerializer()
    dropoff_address = AddressSerializer()
    quote_id = serializers.UUIDField(write_only=True)
    guest_email = serializers.EmailField(required=False, write_only=True)

    class Meta:
        model = Booking
        fields = [
            "id",
            "pickup_address",
            "dropoff_address",
            "quote_id",
            "guest_email",
            "scheduled_pickup_at",
            "scheduled_dropoff_at",
            "promo_code",
            "notes",
        ]
        read_only_fields = ["id"]

    def validate(self, data):
        user = self.context["request"].user if self.context["request"].user.is_authenticated else None
        if not user and not data.get("guest_email"):
            raise serializers.ValidationError(
                "guest_email is required for unauthenticated users")
        quote = Quote.objects.get(pk=data["quote_id"])
        return data

    def create(self, validated_data):
        user = self.context["request"].user if self.context["request"].user.is_authenticated else None
        pickup_data = validated_data.pop("pickup_address")
        dropoff_data = validated_data.pop("dropoff_address")
        quote_id = validated_data.pop("quote_id")
        guest_email = validated_data.pop("guest_email", None)

        quote = Quote.objects.get(pk=quote_id)
        pickup = Address.objects.create(**pickup_data)
        dropoff = Address.objects.create(**dropoff_data)

        booking_data = {
            "customer": user,
            "pickup_address": pickup,
            "dropoff_address": dropoff,
            "quote": quote,
            "final_price": quote.final_price,
            "discount_applied": quote.discount_amount,
            **validated_data,
        }
        if not user:
            booking_data["guest_identifier"] = f"guest-{uuid.uuid4()}"
            booking_data["guest_email"] = guest_email

        booking = Booking.objects.create(**booking_data)
        # to add later: confirmation email to guest_email with guest_identifier
        return booking


class BookingSerializer(serializers.ModelSerializer):
    pickup_address = AddressSerializer()
    dropoff_address = AddressSerializer()
    quote = QuoteSerializer(read_only=True)
    customer = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            "id",
            "customer",
            "guest_identifier",
            "guest_email",
            "driver",
            "status",
            "final_price",
            "pickup_address",
            "dropoff_address",
            "scheduled_pickup_at",
            "scheduled_dropoff_at",
            "promo_code",
            "discount_applied",
            "created_at",
            "updated_at",
            "notes",
            "quote",
        ]

    def get_customer(self, obj):
        return {"id": str(obj.customer_id), "name": getattr(obj.customer, "full_name", None)}


class RecurringScheduleSerializer(serializers.ModelSerializer):
    pickup_address = AddressSerializer(required=False)
    dropoff_address = AddressSerializer(required=False)

    class Meta:
        model = RecurringSchedule
        fields = [
            "id",
            "customer",
            "quote",
            "booking",
            "pickup_address",
            "dropoff_address",
            "recurrence",
            "next_run_at",
            "active",
            "created_at",
            "updated_at",
        ]

    def validate(self, data):
        if not data.get("quote") and not data.get("booking"):
            raise serializers.ValidationError(
                "Either quote or booking must be provided")
        return data

    def create(self, validated_data):
        user = self.context["request"].user
        pickup_data = validated_data.pop("pickup_address", None)
        dropoff_data = validated_data.pop("dropoff_address", None)
        pickup = Address.objects.create(**pickup_data) if pickup_data else None
        dropoff = Address.objects.create(
            **dropoff_data) if dropoff_data else None
        return RecurringSchedule.objects.create(
            customer=user,
            pickup_address=pickup,
            dropoff_address=dropoff,
            **validated_data
        )


class BulkUploadSerializer(serializers.ModelSerializer):
    class Meta:
        model = BulkUpload
        fields = ["id", "customer", "csv_file", "created_at",
                  "processed", "processed_at", "result"]
        read_only_fields = ["id", "customer", "created_at",
                            "processed", "processed_at", "result"]
