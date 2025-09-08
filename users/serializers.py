from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import (
    CustomerProfile,
    AdminProfile,
)

User = get_user_model()


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)
    password = serializers.CharField(required=True, write_only=True)


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

    def validate_email(self, value):
        value = value.lower()
        if User.objects.filter(email=value).exists():
            user = User.objects.get(email=value)
            if user.is_active:
                raise serializers.ValidationError({
                    "code": "ACCOUNT_ALREADY_EXISTS",
                    "error": "Account already exists. Please sign in."
                })
            else:
                raise serializers.ValidationError({
                    "code": "ACCOUNT_NOT_ACTIVATED",
                    "error": "Account exists but is not activated. Please confirm your email."
                })
        return value

    def create(self, validated_data):
        user = User(
            email=validated_data['email'],
            full_name=validated_data['full_name'],
            phone=validated_data.get('phone', ''),
        )
        user.set_password(validated_data['password'])
        user.is_active = False  # Ensure inactive until confirmed
        user.save()
        return user


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField()
    new_password = serializers.CharField(min_length=8)


class CustomerProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerProfile
        fields = ["default_pickup_address",
                  "default_dropoff_address", "preferred_payment_method"]


class AdminProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = AdminProfile
        fields = ["department", "access_level"]
