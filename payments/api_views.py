from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from django.db import transaction
import uuid

from .models import PaymentMethod, PaymentTransaction, Wallet, Refund, PaymentStatus
from .serializers import PaymentMethodSerializer, PaymentTransactionSerializer, WalletSerializer, RefundSerializer
from .permissions import IsCustomer, IsAdmin


class PaymentMethodViewSet(viewsets.ModelViewSet):
    serializer_class = PaymentMethodSerializer

    def get_queryset(self):
        return PaymentMethod.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class PaymentTransactionViewSet(viewsets.ModelViewSet):
    serializer_class = PaymentTransactionSerializer

    def get_queryset(self):
        u = self.request.user
        if getattr(u, "role", None) == "admin":
            return PaymentTransaction.objects.all()
        return PaymentTransaction.objects.filter(user=u)

    def perform_create(self, serializer):
        serializer.save(
            user=self.request.user,
            reference=str(uuid.uuid4())[:12].replace("-", ""),
        )

    @action(methods=["post"], detail=True, url_path="mark-success")
    def mark_success(self, request, pk=None):
        tx = self.get_object()
        tx.status = PaymentStatus.SUCCESS
        tx.updated_at = timezone.now()
        tx.save(update_fields=["status", "updated_at"])
        # Update wallet
        wallet, _ = Wallet.objects.get_or_create(user=tx.user)
        wallet.balance += tx.amount
        wallet.save(update_fields=["balance", "updated_at"])
        return Response(PaymentTransactionSerializer(tx).data)


class WalletViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = WalletSerializer

    def get_queryset(self):
        return Wallet.objects.filter(user=self.request.user)


class RefundViewSet(viewsets.ModelViewSet):
    serializer_class = RefundSerializer

    def get_queryset(self):
        return Refund.objects.all()

    def perform_create(self, serializer):
        refund = serializer.save()
        tx = refund.transaction
        tx.status = PaymentStatus.REFUNDED
        tx.save(update_fields=["status"])
        # Update wallet balance
        wallet, _ = Wallet.objects.get_or_create(user=tx.user)
        wallet.balance -= refund.amount
        wallet.save(update_fields=["balance", "updated_at"])
