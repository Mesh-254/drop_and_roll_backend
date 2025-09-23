# business/serializers.py
from rest_framework import serializers

from bookings.models import ShippingType, ServiceType
from .models import BusinessInquiry, BusinessPricing, BusinessInquiryStatus
from bookings.serializers import QuoteSerializer, BookingSerializer, ShippingTypeSerializer, ServiceTypeSerializer


class BusinessPricingSerializer(serializers.ModelSerializer):
    shipping_type = ShippingTypeSerializer(read_only=True)
    shipping_type_id = serializers.UUIDField(write_only=True)
    service_type = ServiceTypeSerializer(read_only=True)
    service_type_id = serializers.UUIDField(write_only=True)

    class Meta:
        model = BusinessPricing
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def create(self, validated_data):
        shipping_type_id = validated_data.pop('shipping_type_id')
        service_type_id = validated_data.pop('service_type_id')
        validated_data['shipping_type'] = ShippingType.objects.get(id=shipping_type_id)
        validated_data['service_type'] = ServiceType.objects.get(id=service_type_id)
        return super().create(validated_data)


class BusinessInquirySerializer(serializers.ModelSerializer):
    shipping_type = ShippingTypeSerializer(read_only=True)
    shipping_type_id = serializers.UUIDField(write_only=True, required=False)
    service_type = ServiceTypeSerializer(read_only=True)
    service_type_id = serializers.UUIDField(write_only=True, required=False)
    quote = QuoteSerializer(read_only=True)
    booking = BookingSerializer(read_only=True)

    class Meta:
        model = BusinessInquiry
        fields = '__all__'
        read_only_fields = ['id', 'status', 'quote', 'booking', 'created_at', 'updated_at', 'user']

    def create(self, validated_data):
        shipping_type_id = validated_data.pop('shipping_type_id', None)
        service_type_id = validated_data.pop('service_type_id', None)

        if shipping_type_id:
            validated_data['shipping_type'] = ShippingType.objects.get(id=shipping_type_id)
        if service_type_id:
            validated_data['service_type'] = ServiceType.objects.get(id=service_type_id)

        request = self.context.get('request')
        if request and request.user.is_authenticated:
            validated_data['user'] = request.user

        return super().create(validated_data)