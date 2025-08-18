from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import (
    TrackingSession,
    TrackingEvent,
    DriverLocation,
    Geofence,
    ProofOfDelivery,
    WebhookSubscription,
)

User = get_user_model()


class TrackingEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrackingEvent
        fields = ["id", "code", "message", "created_at", "meta"]
        read_only_fields = ["id", "created_at"]


class DriverLocationCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverLocation
        fields = ["lat", "lng", "speed_kph", "heading", "accuracy_m", "recorded_at"]


class DriverLocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverLocation
        fields = [
            "id", "lat", "lng", "speed_kph", "heading", "accuracy_m", "recorded_at", "driver"
        ]
        read_only_fields = ["id", "driver"]


class ProofOfDeliverySerializer(serializers.ModelSerializer):
    class Meta:
        model = ProofOfDelivery
        fields = [
            "id", "recipient_name", "recipient_phone", "signed_at", "signature", "photo", "notes"
        ]


class TrackingSessionSerializer(serializers.ModelSerializer):
    events = TrackingEventSerializer(many=True, read_only=True)
    last_location = serializers.SerializerMethodField()

    class Meta:
        model = TrackingSession
        fields = [
            "id", "booking", "status", "started_at", "ended_at", "last_event_at", "eta",
            "public_token", "public_enabled", "meta", "created_at", "updated_at",
            "events", "last_location",
        ]
        read_only_fields = ["public_token", "created_at", "updated_at"]

    def get_last_location(self, obj: TrackingSession):
        loc = obj.locations.first()
        if not loc:
            return None
        return DriverLocationSerializer(loc).data


class TrackingSessionCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrackingSession
        fields = ["booking", "status", "eta", "public_enabled", "meta"]


class GeofenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Geofence
        fields = ["id", "name", "center_lat", "center_lng", "radius_m", "active", "created_at"]
        read_only_fields = ["id", "created_at"]


class WebhookSubscriptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookSubscription
        fields = ["id", "url", "secret", "active", "created_at"]
        read_only_fields = ["id", "created_at"]
