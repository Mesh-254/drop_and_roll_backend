from rest_framework import serializers

from payments.models import PaymentMethod, PaymentTransaction, Wallet, Refund


class PaymentMethodSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentMethod
        fields = ["id", "method_type", "provider", "account_ref", "is_default", "created_at"]
        read_only_fields = ["id", "created_at"]


class PaymentTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentTransaction
        fields = ["id", "user", "method", "booking", "amount", "status", "reference", "metadata", "created_at",
                  "updated_at"]
        read_only_fields = ["id", "status", "created_at", "updated_at"]


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
