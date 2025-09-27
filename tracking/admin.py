
from django.contrib import admin
from unfold.admin import ModelAdmin
from tracking.models import TrackingSession, TrackingEvent, DriverLocation, Geofence, ProofOfDelivery, WebhookSubscription

@admin.register(TrackingSession)
class TrackingSessionAdmin(ModelAdmin):
    list_display = ("id", "booking", "status", "eta", "created_at", "updated_at")
    list_filter = ("status",)
    search_fields = ("id", "booking__id")

@admin.register(TrackingEvent)
class TrackingEventAdmin(ModelAdmin):
    list_display = ("session", "code", "message", "created_at")
    list_filter = ("code",)
    search_fields = ("session__id", "message")

@admin.register(DriverLocation)
class DriverLocationAdmin(ModelAdmin):
    list_display = ("session", "driver", "lat", "lng", "recorded_at")
    list_filter = ("recorded_at",)
    search_fields = ("session__id", "driver__email")

@admin.register(Geofence)
class GeofenceAdmin(ModelAdmin):
    list_display = ("name", "center_lat", "center_lng", "radius_m", "active")
    list_filter = ("active",)
    search_fields = ("name",)


@admin.register(ProofOfDelivery)
class ProofOfDeliveryAdmin(ModelAdmin):
    list_display = ("booking_id", "created_at",  "notes","location","photo","created_at")
    search_fields = ("booking__id", "notes","location")
    list_filter = ("created_at","location")


@admin.register(WebhookSubscription)
class WebhookSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("customer", "url", "active", "created_at")
    list_filter = ("active",)
    search_fields = ("customer__email", "url")
