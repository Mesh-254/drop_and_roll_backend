from rest_framework import serializers
from .models import Ticket, TicketComment, TicketAttachment, TicketType, TicketPriority, TicketStatus
from bookings.serializers import BookingSerializer  # For nested booking info
from users.serializers import UserSerializer  # For nested user info


class TicketCommentSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)

    class Meta:
        model = TicketComment
        fields = ["id", "user", "content", "is_internal", "created_at"]
        read_only_fields = ["id", "created_at"]


class TicketAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = TicketAttachment
        fields = ["id", "file", "uploaded_by", "created_at"]
        read_only_fields = ["id", "created_at", "uploaded_by"]


class TicketSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    booking = BookingSerializer(read_only=True)  # Nested for context
    comments = TicketCommentSerializer(many=True, read_only=True)
    attachments = TicketAttachmentSerializer(many=True, read_only=True)
    guest_email = serializers.EmailField(
        required=False, write_only=True)  # For guests

    class Meta:
        model = Ticket
        fields = [
            "id", "user", "guest_email", "booking", "type", "priority", "status", "subject", "description",
            "metadata", "created_at", "updated_at", "created_by", "assigned_to", "comments", "attachments"
        ]
        read_only_fields = ["id", "created_at", "updated_at", "created_by"]

    def validate(self, data):
        request = self.context["request"]
        user = request.user if request.user.is_authenticated else None
        if user and data.get("guest_email"):
            raise serializers.ValidationError(
                "Authenticated users cannot provide guest_email.")
        if not user and not data.get("guest_email"):
            raise serializers.ValidationError(
                "guest_email is required for unauthenticated users.")
        return data

    def create(self, validated_data):
        request = self.context["request"]
        user = request.user if request.user.is_authenticated else None
        guest_email = validated_data.pop("guest_email", None)
        ticket = Ticket.objects.create(
            user=user, guest_email=guest_email.lower() if guest_email else None,
            created_by=user, **validated_data
        )
        return ticket
