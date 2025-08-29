from django.contrib.auth import get_user_model
from rest_framework import serializers

from driver.models import (
    DriverAvailability, DriverPayout, DriverRating
)

User = get_user_model()


class DriverAvailabilitySerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverAvailability
        fields = ["id", "available", "lat", "lng", "last_updated"]
        read_only_fields = ["id", "last_updated"]


class DriverPayoutSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverPayout
        fields = ["id", "amount", "status", "payout_date", "meta", "created_at"]
        read_only_fields = ["id", "status", "payout_date", "created_at"]


class DriverPayoutCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverPayout
        fields = ["driver_profile", "amount", "meta"]


class DriverRatingSerializer(serializers.ModelSerializer):
    customer_email = serializers.EmailField(source="customer.email", read_only=True)

    class Meta:
        model = DriverRating
        fields = ["id", "driver_profile", "customer", "booking", "rating", "comment", "created_at", "customer_email"]
        read_only_fields = ["id", "created_at", "customer_email"]

    def validate_rating(self, value):
        if not (1 <= value <= 5):
            raise serializers.ValidationError("Rating must be between 1 and 5")
        return value
