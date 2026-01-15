from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import serializers

from driver.models import (
    DriverAvailability,
    DriverPayout,
    DriverRating,
    DriverDocument,
    DriverProfile,
    DriverInvitation,
    DriverShift,
    DriverLocation,
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
        fields = [
            "id",
            "driver_profile",
            "customer",
            "booking",
            "rating",
            "comment",
            "created_at",
            "customer_email",
        ]
        read_only_fields = ["id", "created_at", "customer_email"]

    def validate_rating(self, value):
        if not (1 <= value <= 5):
            raise serializers.ValidationError("Rating must be between 1 and 5")
        return value


class DriverProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverProfile
        fields = [
            "license_number",
            "vehicle_type",
            "vehicle_registration",
            "hub",
            "status",
            "is_verified",
            "total_deliveries",
            "rating",
        ]
        read_only_fields = ["is_verified", "total_deliveries", "rating"]


class DriverDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverDocument
        fields = ["id", "doc_type", "file", "uploaded_at", "verified", "notes"]
        read_only_fields = ["id", "uploaded_at", "verified"]

    def validate_file(self, value):
        print(
            f"Server received file: name={value.name}, content_type={value.content_type}, size={value.size}"
        )
        allowed_types = ["application/pdf", "image/jpeg", "image/png"]
        if value.content_type not in allowed_types:
            raise serializers.ValidationError(
                f"Invalid file type. Allowed types are: {', '.join(allowed_types)}"
            )
        max_size = 5 * 1024 * 1024
        if value.size > max_size:
            raise serializers.ValidationError(
                f"File size exceeds limit of {max_size / (1024 * 1024)}MB"
            )
        return value

    def validate_doc_type(self, value):
        # Ensure doc_type is one of the expected types
        allowed_doc_types = ["Driver's License", "Vehicle Registration", "Insurance"]
        if value not in allowed_doc_types:
            raise serializers.ValidationError(
                f"Invalid document type. Allowed types are: {', '.join(allowed_doc_types)}"
            )
        return value


class DriverInviteCreateSerializer(serializers.ModelSerializer):
    expires_in_hours = serializers.IntegerField(
        write_only=True, required=False, default=72
    )

    class Meta:
        model = DriverInvitation
        fields = ["id", "email", "full_name", "expires_in_hours"]

    def create(self, validated_data):
        hours = validated_data.pop("expires_in_hours", 72)
        inv = DriverInvitation.objects.create(
            created_by=self.context["request"].user,
            expires_at=timezone.now() + timedelta(hours=hours),
            **validated_data,
        )
        return inv


class DriverInviteDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverInvitation
        fields = ["id", "email", "full_name", "token", "expires_at", "accepted_at"]


class DriverInviteAcceptSerializer(serializers.Serializer):
    token = serializers.UUIDField()
    password = serializers.CharField(min_length=8)
    phone = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        try:
            inv = DriverInvitation.objects.get(token=attrs["token"])
        except DriverInvitation.DoesNotExist:
            raise serializers.ValidationError("Invalid token")
        if inv.accepted_at is not None:
            raise serializers.ValidationError("Invitation already accepted")
        if inv.is_expired():
            raise serializers.ValidationError("Invitation expired")
        attrs["invitation"] = inv
        return attrs

    def create(self, validated_data):
        inv: DriverInvitation = validated_data["invitation"]
        user = User.objects.create_user(
            email=inv.email,
            password=validated_data["password"],
            full_name=inv.full_name,
            phone=validated_data.get("phone"),
            role=User.Role.DRIVER,
        )
        inv.accepted_at = timezone.now()
        inv.save(update_fields=["accepted_at"])
        return user


class DriverInviteSerializer(serializers.Serializer):
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    full_name = serializers.CharField(max_length=255)
    vehicle_type = serializers.ChoiceField(
        choices=DriverProfile.Vehicle, required=False
    )
    license_number = serializers.CharField(
        max_length=50, required=False, allow_blank=True
    )

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def create(self, validated_data):
        """
        Instead of creating a full driver account now, we can:
        - create a pending user with role='driver' and is_active=False
        - OR send an invite link via email with a token
        """
        user = User.objects.create_user(
            email=validated_data["email"],
            phone=validated_data.get("phone"),
            full_name=validated_data["full_name"],
            role=User.Role.DRIVER,
            is_active=False,  # activate when invite is accepted
        )
        # Optional: create a driver profile with partial info
        DriverProfile.objects.create(
            user=user,
            vehicle_type=validated_data.get("vehicle_type", ""),
            license_number=validated_data.get("license_number", ""),
            is_verified=False,
            status="inactive",
        )
        return user


class DriverShiftSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverShift
        fields = ["__all__"]
        read_only_fields = ["id", "current_load"]


# NEW: Create serializer for location updates without driver_profile
class DriverLocationCreateSerializer(serializers.Serializer):
    latitude = serializers.FloatField(required=True)
    longitude = serializers.FloatField(required=True)
    speed_kmh = serializers.FloatField(allow_null=True, required=False)
    heading_degrees = serializers.FloatField(allow_null=True, required=False)
    accuracy_meters = serializers.FloatField(allow_null=True, required=False)
    altitude_meters = serializers.FloatField(allow_null=True, required=False)
    source = serializers.CharField(max_length=20, default="mobile_app", required=False)


class DriverLocationSerializer(serializers.ModelSerializer):
    driver_name = serializers.CharField(
        source="driver_profile.user.get_full_name", read_only=True
    )
    driver_email = serializers.EmailField(
        source="driver_profile.user.email", read_only=True
    )
    vehicle_type = serializers.CharField(
        source="driver_profile.vehicle_type", read_only=True
    )
    vehicle_registration = serializers.CharField(
        source="driver_profile.vehicle_registration", read_only=True, allow_null=True
    )
    hub_name = serializers.CharField(
        source="driver_profile.hub.name", read_only=True, allow_null=True
    )

    class Meta:
        model = DriverLocation
        fields = [
            "id",
            "driver_profile",
            "driver_name",
            "driver_email",
            "vehicle_type",
            "vehicle_registration",
            "hub_name",
            "latitude",
            "longitude",
            "speed_kmh",
            "heading_degrees",
            "accuracy_meters",
            "altitude_meters",
            "source",
            "timestamp",
        ]
        read_only_fields = [
            "id",
            "timestamp",
            "driver_name",
            "driver_email",
            "vehicle_type",
            "vehicle_registration",
            "hub_name",
        ]
