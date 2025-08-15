from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import serializers
from datetime import timedelta

from .models import (
    CustomerProfile,
    DriverProfile,
    AdminProfile,
    DriverDocument,
    DriverInvitation,
)

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "email", "phone", "full_name", "role", "date_joined", "loyalty_points", "is_active"]
        read_only_fields = ["id", "date_joined", "loyalty_points", "is_active", "role"]


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ["id", "email", "phone", "full_name", "password"]

    def create(self, validated_data):
        return User.objects.create_user(
            email=validated_data["email"],
            password=validated_data["password"],
            full_name=validated_data["full_name"],
            phone=validated_data.get("phone"),
            role=User.Role.CUSTOMER,
        )


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField()
    new_password = serializers.CharField(min_length=8)


class CustomerProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerProfile
        fields = ["default_pickup_address", "default_dropoff_address", "preferred_payment_method"]


class DriverProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverProfile
        fields = [
            "license_number",
            "vehicle_type",
            "vehicle_registration",
            "is_verified",
            "status",
            "total_deliveries",
            "rating",
        ]
        read_only_fields = ["is_verified", "total_deliveries", "rating"]


class AdminProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = AdminProfile
        fields = ["department", "access_level"]


class DriverDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverDocument
        fields = ["id", "doc_type", "file", "uploaded_at", "verified", "notes"]
        read_only_fields = ["id", "uploaded_at", "verified"]


class DriverInviteCreateSerializer(serializers.ModelSerializer):
    expires_in_hours = serializers.IntegerField(write_only=True, required=False, default=72)

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
