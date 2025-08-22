from rest_framework import serializers
from .models import Address, Quote, Booking, RecurringSchedule, BulkUpload, ServiceTier, BookingStatus, ShipmentType
from .utils.pricing import compute_quote
from decimal import Decimal


class AddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = Address
        fields = ["id", "line1", "line2", "city", "region",
                  "postal_code", "country", "latitude", "longitude", "validated"]
        read_only_fields = ["id", "validated"]


class QuoteRequestSerializer(serializers.Serializer):
    shipment_type = serializers.ChoiceField(
        choices=ShipmentType.choices, required=True)
    service_tier = serializers.ChoiceField(choices=ServiceTier.choices)
    weight_kg = serializers.DecimalField(
        max_digits=6, decimal_places=2, min_value=Decimal("0"))
    distance_km = serializers.DecimalField(
        max_digits=7, decimal_places=2, min_value=Decimal("0"))
    fragile = serializers.BooleanField(default=False)
    insurance_amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0"), default=Decimal("0"))
    dimensions = serializers.JSONField(default=dict)
    surge = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, default=Decimal("1.00"))
    discount = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, default=Decimal("0.00"))


class QuoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Quote
        fields = "__all__"


class BookingCreateSerializer(serializers.ModelSerializer):
    pickup_address = AddressSerializer()
    dropoff_address = AddressSerializer()
    quote_id = serializers.UUIDField(write_only=True)

    class Meta:
        model = Booking
        fields = [
            "id",
            "shipment_type",
            "service_tier",
            "weight_kg",
            "distance_km",
            "fragile",
            "insurance_amount",
            "dimensions",
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
            shipment_type=quote.shipment_type,
            service_tier=quote.service_tier,
            weight_kg=quote.weight_kg,
            distance_km=quote.distance_km,
            fragile=quote.fragile,
            insurance_amount=quote.insurance_amount,
            dimensions=quote.dimensions,
            final_price=quote.final_price,
            discount_applied=quote.discount_amount,
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
            "shipment_type",
            "service_tier",
            "status",
            "weight_kg",
            "distance_km",
            "fragile",
            "insurance_amount",
            "dimensions",
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
        return RecurringSchedule.objects.create(customer=user, pickup_address=pickup, dropoff_address=dropoff, **validated_data)


class BulkUploadSerializer(serializers.ModelSerializer):
    class Meta:
        model = BulkUpload
        fields = ["id", "customer", "csv_file", "created_at",
                  "processed", "processed_at", "result"]
        read_only_fields = ["id", "customer", "created_at",
                            "processed", "processed_at", "result"]
