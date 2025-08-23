from rest_framework import serializers
from .models import Address, Quote, Booking, RecurringSchedule, BulkUpload, ServiceTier, BookingStatus
from .utils.pricing import compute_quote
from decimal import Decimal

from rest_framework import serializers

from bookings.models import Address, Quote, Booking, RecurringSchedule, BulkUpload, ServiceTier, ShippingType, \
    ServiceType


class ShippingTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShippingType
        fields = ["id", "name", "description", "created_at"]


class ServiceTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceType
        fields = ["id", "name", "type", "price", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class AddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = Address
        fields = ["id", "line1", "line2", "city", "region", "postal_code", "country", "latitude", "longitude",
                  "validated"]
        read_only_fields = ["id", "validated"]


class QuoteRequestSerializer(serializers.Serializer):
    service_tier = serializers.ChoiceField(choices=ServiceTier.choices)
    weight_kg = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=Decimal("0"))
    distance_km = serializers.DecimalField(max_digits=7, decimal_places=2, min_value=Decimal("0"))
    surge = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, default=Decimal("1.00"))
    discount = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, default=Decimal("0.00"))


class QuoteSerializer(serializers.ModelSerializer):
    # Nested serializers for read
    shipping_type = ShippingTypeSerializer(read_only=True)
    service_type = ServiceTypeSerializer(read_only=True)

    # Write-only IDs for POST/PUT
    shipping_type_id = serializers.UUIDField(write_only=True, required=False, allow_null=True)
    service_type_id = serializers.UUIDField(write_only=True, required=False, allow_null=True)

    class Meta:
        model = Quote
        fields = [
            "id",
            "created_at",
            "service_tier",
            "weight_kg",
            "distance_km",
            "insurance",
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
        read_only_fields = ["id", "created_at", "final_price"]

    def _calculate_final_price(self, data: dict) -> Decimal:
        """Compute final price dynamically."""
        base_price = Decimal(data.get("base_price", 0))
        surge_multiplier = Decimal(data.get("surge_multiplier", 1))
        discount = Decimal(data.get("discount_amount", 0))
        insurance = Decimal(data.get("insurance") or 0)

        final_price = base_price * surge_multiplier - discount + insurance
        # Ensure no negative final price
        if final_price < 0:
            final_price = Decimal("0.00")
        return final_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def create(self, validated_data):
        shipping_type_id = validated_data.pop("shipping_type_id", None)
        service_type_id = validated_data.pop("service_type_id", None)

        validated_data["final_price"] = self._calculate_final_price(validated_data)
        quote = Quote.objects.create(**validated_data)

        if shipping_type_id:
            quote.shipping_type_id = shipping_type_id
        if service_type_id:
            quote.service_type_id = service_type_id

        quote.save()
        return quote


class BookingCreateSerializer(serializers.ModelSerializer):
    pickup_address = AddressSerializer()
    dropoff_address = AddressSerializer()
    quote_id = serializers.UUIDField(write_only=True)

    class Meta:
        model = Booking
        fields = [
            "id",
            "service_tier",
            "weight_kg",
            "distance_km",
            "pickup_address",
            "dropoff_address",
            "quote_id",
            "scheduled_pickup_at",
            "scheduled_dropoff_at",
            "promo_code",
            "notes",
        ]
        read_only_fields = ["id"]

    def create(self, validated_data):
        # set user to allow customer to be null or set to a "guest" user.
        user = self.context["request"].user if self.context["request"].user.is_authenticated else None
        pickup_data = validated_data.pop("pickup_address")
        dropoff_data = validated_data.pop("dropoff_address")
        quote_id = validated_data.pop("quote_id")

        pickup = Address.objects.create(**pickup_data)
        dropoff = Address.objects.create(**dropoff_data)

        quote = Quote.objects.get(pk=quote_id)
        booking = Booking.objects.create(
            customer=user,
            pickup_address=pickup,
            dropoff_address=dropoff,
            quote=quote,
            final_price=quote.final_price,
            **validated_data,
        )
        return booking


class BookingSerializer(serializers.ModelSerializer):
    pickup_address = AddressSerializer()
    dropoff_address = AddressSerializer()
    customer = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            "id",
            "customer",
            "driver",
            "service_tier",
            "status",
            "weight_kg",
            "distance_km",
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
        ]

    def get_customer(self, obj):
        return {"id": str(obj.customer_id), "name": getattr(obj.customer, "full_name", None)}


class RecurringScheduleSerializer(serializers.ModelSerializer):
    pickup_address = AddressSerializer()
    dropoff_address = AddressSerializer()

    class Meta:
        model = RecurringSchedule
        fields = "__all__"

    def create(self, validated_data):
        user = self.context["request"].user
        pickup_data = validated_data.pop("pickup_address")
        dropoff_data = validated_data.pop("dropoff_address")
        pickup = Address.objects.create(**pickup_data)
        dropoff = Address.objects.create(**dropoff_data)
        return RecurringSchedule.objects.create(customer=user, pickup_address=pickup, dropoff_address=dropoff,
                                                **validated_data)


class BulkUploadSerializer(serializers.ModelSerializer):
    class Meta:
        model = BulkUpload
        fields = ["id", "customer", "csv_file", "created_at",
                  "processed", "processed_at", "result"]
        read_only_fields = ["id", "customer", "created_at",
                            "processed", "processed_at", "result"]
