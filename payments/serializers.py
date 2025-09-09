from rest_framework import serializers

from payments.models import PaymentMethod, PaymentTransaction, Wallet, Refund


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
    class Meta:
        model = Refund
        fields = ["id", "transaction", "amount", "reason", "refunded_at"]
        read_only_fields = ["id", "refunded_at"]
