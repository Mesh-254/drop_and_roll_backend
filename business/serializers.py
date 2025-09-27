# business/serializers.py
from rest_framework import serializers

from bookings.models import ShippingType, ServiceType, Address
from .models import BusinessInquiry, BusinessPricing, BusinessInquiryStatus
from bookings.serializers import QuoteSerializer, BookingSerializer, ShippingTypeSerializer, ServiceTypeSerializer, \
    AddressSerializer


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
    pickup_address = AddressSerializer(read_only=True)
    pickup_address_id = serializers.UUIDField(write_only=True, required=False, allow_null=True)
    dropoff_address = AddressSerializer(read_only=True)
    dropoff_address_id = serializers.UUIDField(write_only=True, required=False, allow_null=True)

    class Meta:
        model = BusinessInquiry
        fields = [
            'id',
            'business_name',
            'contact_person',
            'email',
            'phone',
            'description',
            'pickup_address',
            'pickup_address_id',
            'dropoff_address',
            'dropoff_address_id',
            'status',
            'admin_notes',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['id', 'status', 'created_at', 'updated_at']

    def create(self, validated_data):
        pickup_address_id = validated_data.pop('pickup_address_id', None)
        dropoff_address_id = validated_data.pop('dropoff_address_id', None)

        if pickup_address_id:
            validated_data['pickup_address'] = Address.objects.get(id=pickup_address_id)
        if dropoff_address_id:
            validated_data['dropoff_address'] = Address.objects.get(id=dropoff_address_id)

        return super().create(validated_data)

    def update(self, instance, validated_data):
        pickup_address_id = validated_data.pop('pickup_address_id', None)
        dropoff_address_id = validated_data.pop('dropoff_address_id', None)

        if pickup_address_id:
            validated_data['pickup_address'] = Address.objects.get(id=pickup_address_id)
        if dropoff_address_id:
            validated_data['dropoff_address'] = Address.objects.get(id=dropoff_address_id)

        return super().update(instance, validated_data)