from django.urls import path, include
from rest_framework.routers import DefaultRouter
from payments.api_views import PaymentMethodViewSet, PaymentTransactionViewSet, WalletViewSet, RefundViewSet

router = DefaultRouter()
router.register(r"methods", PaymentMethodViewSet, basename="payment-methods")
router.register(r"transactions", PaymentTransactionViewSet, basename="payment-transactions")
router.register(r"wallets", WalletViewSet, basename="wallets")
router.register(r"refunds", RefundViewSet, basename="refunds")

urlpatterns = [path("", include(router.urls))]
