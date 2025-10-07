from rest_framework import serializers  # type: ignore
from payments.models import PaymentMethod, PaymentTransaction, Wallet, Refund, PaymentStatus
from .utils import process_refund


class PaymentMethodSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentMethod
        fields = ["id", "method_type", "provider",
                  "account_ref", "is_default", "created_at"]
        read_only_fields = ["id", "created_at"]


class PaymentTransactionSerializer(serializers.ModelSerializer):

    class Meta:
        model = PaymentTransaction
        fields = ["id", "user", "guest_email", "method", "booking", "amount", "status", "gateway_response", "reference", "metadata", "created_at",
                  "updated_at"]
        read_only_fields = ["id", "status", "reference",
                            "created_at", "updated_at", "gateway_response"]


class WalletSerializer(serializers.ModelSerializer):
    class Meta:
        model = Wallet
        fields = ["id", "balance", "updated_at"]
        read_only_fields = ["id", "balance", "updated_at"]


class RefundSerializer(serializers.ModelSerializer):
    transaction = serializers.PrimaryKeyRelatedField(
        queryset=PaymentTransaction.objects.all())
    admin_user = serializers.ReadOnlyField(
        source='admin_user.email')  # Optional: Expose email for API

    class Meta:
        model = Refund
        fields = '__all__'
        # gateway_response set by process_refund
        read_only_fields = ['id', 'status', 'gateway_response', 'refunded_at']

    def create(self, validated_data):
        # Assuming existing logic...
        refund = super().create(validated_data)
        admin_user = self.context.get(
            'request').user if self.context.get('request') else None
        success = process_refund(refund, admin_user=admin_user)
        if not success:
            raise serializers.ValidationError("Refund processing failed.")
        return refund

    def validate(self, data):
        tx = data['transaction']
        amount = data['amount']
        if tx.status != PaymentStatus.SUCCESS:
            raise serializers.ValidationError(
                f"Transaction {tx.reference} not refundable (status: {tx.status}).")
        if amount != tx.amount:  # NEW: Full-only enforcement
            raise serializers.ValidationError(
                f"Partial refunds not supported. Amount must equal original {tx.amount}.")
        if Refund.objects.filter(transaction=tx).exists():  # NEW: Duplicate check
            raise serializers.ValidationError(
                "A refund already exists for this transaction.")
        return data

    def create(self, validated_data):
        refund = Refund.objects.create(**validated_data)
        request = self.context.get('request')
        admin_user = request.user if request else None
        success = process_refund(refund, admin_user=admin_user)
        if not success:
            raise serializers.ValidationError(
                "Refund processing failed—check gateway response.")
        return refund
