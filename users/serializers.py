from .models import DriverProfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import serializers
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.contrib.auth.tokens import default_token_generator
from django.conf import settings
from .tasks import send_confirmation_email

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
        fields = ["id", "email", "phone", "full_name", "role",
                  "date_joined", "loyalty_points", "is_active"]
        read_only_fields = ["id", "date_joined",
                            "loyalty_points", "is_active", "role"]


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ["id", "email", "phone", "full_name", "password"]

    def create(self, validated_data):
        user = User.objects.create_user(
            email=validated_data["email"],
            password=validated_data["password"],
            full_name=validated_data["full_name"],
            phone=validated_data.get("phone"),
            role=User.Role.CUSTOMER,
        )
        # Set inactive until confirmed
        user.is_active = False
        user.save()
        # Send confirmation email
        if user.role == User.Role.CUSTOMER and not user.is_active:
            token = default_token_generator.make_token(user)
            uid = urlsafe_base64_encode(force_bytes(str(user.pk)))
            confirmation_link = f"{settings.FRONTEND_URL}/confirm-email/?uid={uid}&token={token}"

            subject = "Confirm Your Drop 'N Roll Account"
            message = (
                f"Hi {user.full_name},\n\n"
                f"Thank you for registering with Drop 'N Roll! "
                f"Please confirm your email by clicking the link below:\n\n"
                f"{confirmation_link}\n\n"
                f"If you did not create this account, please ignore this email.\n\n"
                f"Best,\nDrop 'N Roll Team"
            )
            from_email = settings.DEFAULT_FROM_EMAIL
            recipient_list = [user.email]

            send_confirmation_email.delay(
                subject, message, from_email, recipient_list)

        return user


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField()
    new_password = serializers.CharField(min_length=8)


class CustomerProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerProfile
        fields = ["default_pickup_address",
                  "default_dropoff_address", "preferred_payment_method"]


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
    expires_in_hours = serializers.IntegerField(
        write_only=True, required=False, default=72)

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
        fields = ["id", "email", "full_name",
                  "token", "expires_at", "accepted_at"]


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


User = get_user_model()


class DriverInviteSerializer(serializers.Serializer):
    email = serializers.EmailField()
    phone = serializers.CharField(
        max_length=20, required=False, allow_blank=True)
    full_name = serializers.CharField(max_length=255)
    vehicle_type = serializers.ChoiceField(
        choices=DriverProfile.Vehicle, required=False)
    license_number = serializers.CharField(
        max_length=50, required=False, allow_blank=True)

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError(
                "A user with this email already exists.")
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
            is_active=False  # activate when invite is accepted
        )
        # Optional: create a driver profile with partial info
        DriverProfile.objects.create(
            user=user,
            vehicle_type=validated_data.get("vehicle_type", ""),
            license_number=validated_data.get("license_number", ""),
            is_verified=False,
            status="inactive"
        )
        return user
