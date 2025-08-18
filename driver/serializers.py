from django.contrib.auth import get_user_model
from rest_framework import serializers
from .models import (
    DriverProfile, DriverDocument, DriverAvailability, DriverPayout, DriverRating, DriverInvite,
    DocumentType, DocumentStatus, DriverStatus, PayoutStatus, InviteStatus
)

User = get_user_model()


class DriverProfileSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    full_name = serializers.CharField(source="user.full_name", read_only=True)

    class Meta:
        model = DriverProfile
        fields = [
            "id", "user", "email", "full_name", "vehicle_type", "license_number",
            "is_verified", "status", "rating_avg", "rating_count", "created_at", "updated_at"
        ]
        read_only_fields = ["id", "user", "is_verified", "rating_avg", "rating_count", "created_at", "updated_at"]


class DriverProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverProfile
        fields = ["vehicle_type", "license_number"]


# class DriverDocumentSerializer(serializers.ModelSerializer):
#     class Meta:
#         model = DriverDocument
#         fields = ["id", "driver_profile", "document_type", "file", "status", "reason", "uploaded_at", "reviewed_at"]
#         read_only_fields = ["status", "reason", "uploaded_at", "reviewed_at"]


class DriverDocumentCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverDocument
        fields = ["document_type", "file"]


class DriverDocumentReviewSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=DocumentStatus.choices)
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)


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


class DriverInviteSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverInvite
        fields = ["id", "email", "invited_by", "token", "status", "sent_at", "accepted_at", "payload"]
        read_only_fields = ["id", "token", "status", "sent_at", "accepted_at"]


class DriverInviteAcceptSerializer(serializers.Serializer):
    token = serializers.UUIDField()
    password = serializers.CharField(write_only=True, min_length=8)

    def validate(self, attrs):
        token = attrs["token"]
        try:
            invite = DriverInvite.objects.get(token=token, status=InviteStatus.PENDING)
        except DriverInvite.DoesNotExist:
            raise serializers.ValidationError({"token": "Invalid or already used invite token"})
        attrs["invite"] = invite
        return attrs

    def create(self, validated_data):
        invite: DriverInvite = validated_data["invite"]
        payload = invite.payload or {}
        # Create or get user
        user = User.objects.filter(email=invite.email).first()
        if user is None:
            user = User.objects.create_user(
                email=invite.email,
                full_name=payload.get("full_name", invite.email.split("@")[0]),
                phone=payload.get("phone"),
                role=getattr(User, "Role").DRIVER if hasattr(User, "Role") else "driver",
                is_active=True,
                password=validated_data["password"],
            )
        else:
            user.set_password(validated_data["password"])
            # Ensure role
            if hasattr(User, "Role"):
                user.role = User.Role.DRIVER
            user.is_active = True
            user.save(update_fields=["password", "is_active", "role"] if hasattr(User, "Role") else ["password", "is_active"])
        # Ensure driver profile
        profile, _ = DriverProfile.objects.get_or_create(
            user=user,
            defaults={
                "vehicle_type": payload.get("vehicle_type", ""),
                "license_number": payload.get("license_number", ""),
                "status": DriverStatus.INACTIVE,
            },
        )
        # Mark invite accepted
        invite.status = InviteStatus.ACCEPTED
        invite.accepted_at = timezone.now()
        invite.save(update_fields=["status", "accepted_at"])
        return profile