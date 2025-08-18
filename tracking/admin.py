
from django.contrib import admin
from tracking.models import TrackingSession, TrackingEvent, DriverLocation, Geofence, ProofOfDelivery, WebhookSubscription

@admin.register(TrackingSession)
class TrackingSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "booking", "status", "eta", "created_at", "updated_at")
    list_filter = ("status",)
    search_fields = ("id", "booking__id")

@admin.register(TrackingEvent)
class TrackingEventAdmin(admin.ModelAdmin):
    list_display = ("session", "code", "message", "created_at")
    list_filter = ("code",)
    search_fields = ("session__id", "message")

@admin.register(DriverLocation)
class DriverLocationAdmin(admin.ModelAdmin):
    list_display = ("session", "driver", "lat", "lng", "recorded_at")
    list_filter = ("recorded_at",)
    search_fields = ("session__id", "driver__email")

@admin.register(Geofence)
class GeofenceAdmin(admin.ModelAdmin):
    list_display = ("name", "center_lat", "center_lng", "radius_m", "active")
    list_filter = ("active",)
    search_fields = ("name",)

@admin.register(ProofOfDelivery)
class ProofOfDeliveryAdmin(admin.ModelAdmin):
    list_display = ("session", "recipient_name", "signed_at")
    search_fields = ("session__id", "recipient_name")

@admin.register(WebhookSubscription)
class WebhookSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("customer", "url", "active", "created_at")
    list_filter = ("active",)
    search_fields = ("customer__email", "url")
