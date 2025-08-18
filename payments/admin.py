from django.contrib import admin
from payments.models import PaymentMethod, PaymentTransaction, Wallet, Refund

@admin.register(PaymentMethod)
class PaymentMethodAdmin(admin.ModelAdmin):
    list_display = ("user", "method_type", "provider", "account_ref", "is_default", "created_at")

@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = ("user", "amount", "status", "reference", "created_at")

@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ("user", "balance", "updated_at")

@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    list_display = ("transaction", "amount", "reason", "refunded_at")
