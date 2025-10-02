from django.conf import settings  # type: ignore
from rest_framework import viewsets, status  # type: ignore
from rest_framework.decorators import action, api_view  # type: ignore
from rest_framework.response import Response  # type: ignore
from django.utils import timezone  # type: ignore
from django.db import transaction  # type: ignore
import uuid
import shortuuid  # type: ignore
from decimal import Decimal

from rest_framework.views import APIView  # type: ignore

import requests
import json
import base64
import zlib
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes
from cryptography import x509
from cryptography.hazmat.backends import default_backend

from bookings.models import BookingStatus
from .models import PaymentMethod, PaymentMethodType, PaymentTransaction, Wallet, Refund, PaymentStatus
from .serializers import PaymentMethodSerializer, PaymentTransactionSerializer, WalletSerializer, RefundSerializer
from .permissions import IsCustomer, IsAdmin
import logging
from rest_framework.permissions import IsAuthenticated, AllowAny  # type: ignore

import stripe  # type: ignore
from django.views.decorators.csrf import csrf_exempt  # type: ignore
from django.http import HttpResponse  # type: ignore

logger = logging.getLogger(__name__)

# stripe api key setup
stripe.api_key = settings.STRIPE_SECRET_KEY


def get_access_token():
    url = f"{settings.PAYPAL_API_URL}/v1/oauth2/token"
    auth = (settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET)
    data = {"grant_type": "client_credentials"}
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en_US",
    }
    try:
        response = requests.post(url, auth=auth, data=data, headers=headers)
        response.raise_for_status()
        logger.debug(f"PayPal token response: {response.json()}")
        return response.json()["access_token"]
    except requests.exceptions.RequestException as e:
        logger.error(
            f"PayPal access token error: {str(e)} - Response: {response.text if 'response' in locals() else 'No response'}")
        raise


def get_certificate(cert_url):
    response = requests.get(cert_url)
    response.raise_for_status()
    return response.text


def verify_webhook_signature(request, webhook_id):
    transmission_id = request.headers.get("PAYPAL-TRANSMISSION-ID")
    timestamp = request.headers.get("PAYPAL-TRANSMISSION-TIME")
    crc = zlib.crc32(request.body)
    message = f"{transmission_id}|{timestamp}|{webhook_id}|{crc}"

    signature = base64.b64decode(
        request.headers.get("PAYPAL-TRANSMISSION-SIG"))

    cert_url = request.headers.get("PAYPAL-CERT-URL")
    certificate = get_certificate(cert_url)
    cert = x509.load_pem_x509_certificate(
        certificate.encode("utf-8"), default_backend())
    public_key = cert.public_key()

    try:
        public_key.verify(
            signature,
            message.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256()
        )
        logger.debug("PayPal webhook signature verified successfully")
        return True
    except Exception as e:
        logger.error(f"PayPal webhook signature verification failed: {str(e)}")
        return False


class PaymentMethodViewSet(viewsets.ModelViewSet):
    serializer_class = PaymentMethodSerializer

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):  # Short-circuit during schema gen
            return PaymentMethod.objects.none()
        
        user = self.request.user
        if not user.is_authenticated:
            return PaymentMethod.objects.none()  # Or raise PermissionDenied if needed
        
        return PaymentMethod.objects.filter(user=user)  # Now safe: user.id is UUID

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class PaymentTransactionViewSet(viewsets.ModelViewSet):
    serializer_class = PaymentTransactionSerializer
    queryset = PaymentTransaction.objects.all()
    permission_classes = [AllowAny]

    def get_queryset(self):
        if self.request.user.is_authenticated:
            return PaymentTransaction.objects.filter(user=self.request.user)
        return PaymentTransaction.objects.all()

    def perform_create(self, serializer):
        serializer.save(
            user=self.request.user,
            reference=str(uuid.uuid4())[:12].replace("-", ""),
        )

    def get_object(self):
        obj = super().get_object()
        if self.request.user.is_authenticated:
            if obj.user != self.request.user:
                self.permission_denied(
                    self.request, message="You do not have permission to access this transaction")
        else:
            guest_email = self.request.query_params.get('guest_email')
            if not guest_email or obj.guest_email != guest_email.lower():
                self.permission_denied(
                    self.request, message="Invalid or missing guest email for anonymous access")
        return obj

    @action(methods=["post"], detail=True, url_path="mark-success")
    def mark_success(self, request, pk=None):
        tx = self.get_object()
        tx.status = PaymentStatus.SUCCESS
        tx.updated_at = timezone.now()
        tx.save(update_fields=["status", "updated_at"])
        if not tx.booking:
            wallet, _ = Wallet.objects.get_or_create(user=tx.user)
            wallet.balance += tx.amount
            wallet.save(update_fields=["balance", "updated_at"])
        else:
            tx.booking.status = BookingStatus.SCHEDULED
            tx.booking.tracking_number = f"BK-{shortuuid.uuid()[:8].upper()}"
            tx.booking.save(update_fields=["status", "tracking_number"])
            logger.info(
                f"Transaction {tx.id} marked success, booking {tx.booking.id} scheduled with tracking {tx.booking.tracking_number}")
        return Response(PaymentTransactionSerializer(tx).data)

    @action(methods=['post'], detail=True, url_path='initiate')
    def initiate_transaction(self, request, pk=None):
        try:
            transaction = self.get_object()
            if transaction.status != PaymentStatus.PENDING:
                logger.warning(
                    f"Transaction {pk} is not pending: {transaction.status}")
                return Response({"success": False, "error": "Transaction is not pending"}, status=status.HTTP_400_BAD_REQUEST)

            exchange_rate_kes_to_usd = Decimal('0.00775')
            usd_amount = transaction.amount * exchange_rate_kes_to_usd
            usd_amount_str = f"{usd_amount:.2f}"

            if usd_amount <= 0:
                logger.error(
                    f"Invalid transaction amount for {pk}: {usd_amount_str} USD (KSh {transaction.amount})")
                return Response({"success": False, "error": "Transaction amount must be greater than zero"}, status=status.HTTP_400_BAD_REQUEST)

            access_token = get_access_token()
            url = f"{settings.PAYPAL_API_URL}/v2/checkout/orders"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}",
            }
            body = {
                "intent": "CAPTURE",
                "purchase_units": [{
                    "amount": {
                        "currency_code": "USD",
                        "value": usd_amount_str
                    },
                    "description": f"Payment for booking {transaction.booking.id if transaction.booking else 'N/A'}",
                    "custom_id": transaction.reference  # Add custom_id for fallback lookup
                }],
                "application_context": {
                    "return_url": f"{settings.PAYPAL_RETURN_URL}/{transaction.id}",
                    "cancel_url": f"{settings.PAYPAL_CANCEL_URL}/{transaction.id}?cancelled=true",
                    "brand_name": "Drop and Roll",
                    "landing_page": "BILLING",
                    "user_action": "PAY_NOW"
                }
            }
            response = requests.post(url, headers=headers, json=body)
            try:
                response.raise_for_status()
                order_data = response.json()
                transaction.metadata['paypal_order_id'] = order_data['id']
                transaction.save(update_fields=['metadata'])
                logger.info(
                    f"Initiated PayPal order {order_data['id']} for transaction {transaction.id}")
                return Response({"success": True, "links": order_data.get('links', [])}, status=status.HTTP_200_OK)
            except requests.exceptions.RequestException as e:
                logger.error(
                    f"PayPal order creation failed for transaction {pk}: {str(e)} - Response: {response.text}")
                return Response({"success": False, "error": "Failed to initiate PayPal order"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            logger.error(f"Error initiating transaction {pk}: {str(e)}")
            return Response({"success": False, "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @api_view(['POST'])
    def cancel_transaction(request, tx_id):
        try:
            transaction = PaymentTransaction.objects.get(id=tx_id)
            if transaction.status != PaymentStatus.PENDING:
                return Response({"error": "Transaction cannot be cancelled"}, status=status.HTTP_400_BAD_REQUEST)
            transaction.status = PaymentStatus.CANCELLED
            transaction.save(update_fields=["status", "updated_at"])
            if transaction.booking:
                transaction.booking.status = BookingStatus.CANCELLED
                transaction.booking.save(
                    update_fields=["status", "updated_at"])
            logger.info(f"Transaction {tx_id} cancelled")
            return Response(PaymentTransactionSerializer(transaction).data, status=status.HTTP_200_OK)
        except PaymentTransaction.DoesNotExist:
            return Response({"error": "Transaction not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"Error cancelling transaction {tx_id}: {str(e)}")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    # Add this to payments/api_views.py, inside PaymentTransactionViewSet

    @action(methods=['post'], detail=True, url_path='capture')
    def capture_transaction(self, request, pk=None):
        try:
            transaction = self.get_object()
            if transaction.status != PaymentStatus.PENDING:
                logger.warning(
                    f"Transaction {pk} is not pending: {transaction.status}")
                return Response({"success": False, "error": "Transaction is not pending"}, status=status.HTTP_400_BAD_REQUEST)

            order_id = transaction.metadata.get('paypal_order_id')
            if not order_id:
                logger.error(f"No PayPal order ID found for transaction {pk}")
                return Response({"success": False, "error": "No PayPal order ID found"}, status=status.HTTP_400_BAD_REQUEST)

            access_token = get_access_token()
            url = f"{settings.PAYPAL_API_URL}/v2/checkout/orders/{order_id}/capture"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}",
            }
            response = requests.post(url, headers=headers)
            try:
                response.raise_for_status()
                capture_data = response.json()
                transaction.status = PaymentStatus.SUCCESS
                transaction.gateway_response = dict(capture_data)
                transaction.metadata['capture_id'] = capture_data.get('id')
                transaction.save(
                    update_fields=["status", "gateway_response", "metadata"])

                if transaction.booking:
                    transaction.booking.status = BookingStatus.SCHEDULED
                    transaction.booking.tracking_number = f"BK-{shortuuid.uuid()[:8].upper()}"
                    transaction.booking.save(
                        update_fields=["status", "tracking_number"])
                    logger.info(
                        f"Captured transaction {transaction.id}, set booking {transaction.booking.id} to SCHEDULED with tracking {transaction.booking.tracking_number}")
                return Response(PaymentTransactionSerializer(transaction).data, status=status.HTTP_200_OK)
            except requests.exceptions.RequestException as e:
                logger.error(
                    f"PayPal capture failed for transaction {pk}: {str(e)} - Response: {response.text}")
                transaction.status = PaymentStatus.FAILED
                transaction.gateway_response = {
                    "error": response.text if 'response' in locals() else str(e)}
                transaction.save(update_fields=["status", "gateway_response"])
                return Response({"success": False, "error": "Capture failed"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            logger.error(f"Error capturing transaction {pk}: {str(e)}")
            return Response({"success": False, "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # STRIPE PAYMENT HANDLER

    @action(methods=['post'], detail=True, url_path='initiate-stripe')
    def initiate_stripe_transaction(self, request, pk=None):
        try:
            transaction = self.get_object()
            if transaction.status != PaymentStatus.PENDING:
                logger.warning(
                    f"Transaction {pk} is not pending: {transaction.status}")
                return Response({"success": False, "error": "Transaction is not pending"}, status=status.HTTP_400_BAD_REQUEST)

            # Skip method validation for one-time Stripe payments (e.g., Apple Pay or CardElement)
            if transaction.method and transaction.method.method_type not in [PaymentMethodType.CARD, PaymentMethodType.STRIPE]:
                logger.error(
                    f"Invalid payment method for Stripe transaction {pk}")
                return Response({"success": False, "error": "A valid CARD payment method is required for Stripe"}, status=status.HTTP_400_BAD_REQUEST)

            exchange_rate_kes_to_usd = Decimal('0.5')
            usd_amount = transaction.amount * exchange_rate_kes_to_usd
            # Convert to cents for Stripe
            usd_amount_cents = int(usd_amount * 100)

            if usd_amount_cents <= 0:
                logger.error(
                    f"Invalid transaction amount for {pk}: {usd_amount} USD (KSh {transaction.amount})")
                return Response({"success": False, "error": "Transaction amount must be greater than zero"}, status=status.HTTP_400_BAD_REQUEST)

            intent = stripe.PaymentIntent.create(
                amount=usd_amount_cents,
                currency="usd",
                payment_method_types=["card"],  # Allow card and Apple Pay
                metadata={"transaction_id": str(transaction.id)},
                description=f"Payment for transaction {transaction.reference}",
            )

            transaction.metadata.update(
                {"gateway": "stripe", "stripe_payment_intent_id": intent.id})
            transaction.save(update_fields=["metadata"])
            logger.info(
                f"Stripe PaymentIntent created for tx {transaction.id}: {intent.id}")

            return Response({
                "success": True,
                "client_secret": intent.client_secret,
            })
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error for tx {pk}: {str(e)}")
            return Response({"success": False, "error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Error initiating Stripe transaction {pk}: {str(e)}")
            return Response({"success": False, "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

# stripe webhook callback handler
@csrf_exempt
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE')
    event = None

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)
    except ValueError:
        logger.error("Invalid Stripe payload")
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError:
        logger.error("Invalid Stripe signature")
        return HttpResponse(status=400)

    with transaction.atomic():
        if event['type'] == 'payment_intent.succeeded':
            intent = event['data']['object']
            tx_id = intent['metadata'].get('transaction_id')
            try:
                tx = PaymentTransaction.objects.get(id=tx_id)
                if tx.metadata.get('gateway') != 'stripe':
                    logger.warning(f"Non-Stripe tx {tx_id} in webhook")
                    return HttpResponse(status=400)
                if tx.status == PaymentStatus.PENDING:
                    tx.status = PaymentStatus.SUCCESS
                    tx.gateway_response.update(dict(intent))
                    tx.save(update_fields=["status", "gateway_response"])
                    if tx.booking:
                        tx.booking.status = BookingStatus.SCHEDULED
                        tx.booking.tracking_number = f"BK-{shortuuid.uuid()[:8].upper()}"
                        tx.booking.save(
                            update_fields=["status", "tracking_number"])
                    logger.info(f"Stripe success for tx {tx.id}")
            except PaymentTransaction.DoesNotExist:
                logger.error(f"Tx {tx_id} not found")

        elif event['type'] == 'payment_intent.payment_failed':
            intent = event['data']['object']
            tx_id = intent['metadata'].get('transaction_id')
            try:
                tx = PaymentTransaction.objects.get(id=tx_id)
                if tx.metadata.get('gateway') != 'stripe':
                    return HttpResponse(status=400)
                tx.status = PaymentStatus.FAILED
                tx.gateway_response.update(dict(intent))
                tx.save(update_fields=["status", "gateway_response"])
                logger.info(f"Stripe failure for tx {tx.id}")
            except PaymentTransaction.DoesNotExist:
                logger.error(f"Tx {tx_id} not found")

    return HttpResponse(status=200)

# paypal webhook callback handler
class PaymentCallbackView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        if not verify_webhook_signature(request, settings.PAYPAL_WEBHOOK_ID):
            logger.error("PayPal webhook signature verification failed")
            return Response({"error": "Invalid signature"}, status=status.HTTP_403_FORBIDDEN)

        data = request.data
        event_type = data.get('event_type')
        resource = data.get('resource')
        logger.debug(
            f"Received PayPal webhook event: {event_type}, resource: {json.dumps(resource, indent=2)}")

        if event_type == 'PAYMENT.CAPTURE.COMPLETED':
            order_id = resource.get('supplementary_data', {}).get(
                'related_ids', {}).get('order_id') or resource.get('id')
            try:
                tx = PaymentTransaction.objects.filter(
                    metadata__paypal_order_id=order_id).first()
                if not tx:
                    tx = PaymentTransaction.objects.filter(
                        reference=resource.get('custom_id') or order_id).first()
                if not tx:
                    logger.error(
                        f"Transaction not found for order_id {order_id}")
                    return Response({"error": "Transaction not found"}, status=status.HTTP_400_BAD_REQUEST)
                if tx.status != PaymentStatus.PENDING:
                    logger.warning(
                        f"Transaction {tx.id} already processed: {tx.status}")
                    return Response({"message": "Already processed"}, status=status.HTTP_200_OK)

                tx.status = PaymentStatus.SUCCESS
                tx.gateway_response = dict(resource)
                tx.metadata['capture_id'] = resource['id']
                tx.save(update_fields=[
                        "status", "gateway_response", "metadata"])

                if tx.booking:
                    tx.booking.status = BookingStatus.SCHEDULED
                    tx.booking.tracking_number = f"BK-{shortuuid.uuid()[:8].upper()}"
                    tx.booking.save(
                        update_fields=["status", "tracking_number"])
                    logger.info(
                        f"Webhook updated transaction {tx.id} to SUCCESS, booking {tx.booking.id} to SCHEDULED with tracking {tx.booking.tracking_number}")
                return Response({"message": "Webhook processed"}, status=status.HTTP_200_OK)
            except Exception as e:
                logger.error(
                    f"Error processing webhook for order_id {order_id}: {str(e)}")
                return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        elif event_type in ['PAYMENT.CAPTURE.DENIED', 'PAYMENT.CAPTURE.REFUNDED']:
            order_id = resource.get('supplementary_data', {}).get(
                'related_ids', {}).get('order_id') or resource.get('id')
            tx = PaymentTransaction.objects.filter(
                metadata__paypal_order_id=order_id).first()
            if not tx:
                tx = PaymentTransaction.objects.filter(
                    reference=resource.get('custom_id') or order_id).first()
            if tx and tx.status == PaymentStatus.PENDING:
                tx.status = PaymentStatus.FAILED
                tx.gateway_response = dict(resource)
                tx.save(update_fields=["status", "gateway_response"])
                logger.info(f"Webhook updated transaction {tx.id} to FAILED")
            return Response({"message": "Webhook processed"}, status=status.HTTP_200_OK)
        else:
            logger.debug(f"Ignored webhook event: {event_type}")
            return Response({"message": "Event ignored"}, status=status.HTTP_200_OK)


class WalletViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = WalletSerializer

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Wallet.objects.none()
        
        user = self.request.user
        if not user.is_authenticated:
            return Wallet.objects.none()
        
        return Wallet.objects.filter(user=user)


class RefundViewSet(viewsets.ModelViewSet):
    serializer_class = RefundSerializer
    permission_classes = [IsCustomer | IsAdmin]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Refund.objects.none()
        
        user = self.request.user
        if user.is_authenticated and user.is_staff:
            return Refund.objects.all()
        return Refund.objects.filter(transaction__user=user) | Refund.objects.filter(transaction__guest_email=user.email.lower())

    def perform_create(self, serializer):
        with transaction.atomic():
            refund = serializer.save()
            tx = refund.transaction
            tx.status = PaymentStatus.REFUNDED
            gateway = tx.metadata.get('gateway')
            if gateway == 'paypal':
                capture_id = tx.metadata.get('capture_id')
                if not capture_id:
                    raise ValueError("No capture ID found for refund")
                access_token = get_access_token()
                url = f"{settings.PAYPAL_API_URL}/v2/payments/captures/{capture_id}/refund"
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {access_token}",
                }
                body = {
                    "amount": {"value": str(refund.amount), "currency_code": "USD"}
                }
                response = requests.post(url, headers=headers, json=body)
                try:
                    response.raise_for_status()
                    tx.gateway_response['refund_response'] = response.json()
                except requests.exceptions.RequestException as e:
                    tx.gateway_response['refund_error'] = response.text if 'response' in locals(
                    ) else str(e)
                    raise
            elif gateway == 'stripe':
                intent_id = tx.metadata.get('stripe_payment_intent_id')
                if not intent_id:
                    raise ValueError(
                        "No Stripe PaymentIntent ID found for refund")
                stripe_refund = stripe.Refund.create(
                    payment_intent=intent_id,
                    amount=int(refund.amount * 100),  # Convert to cents
                )
                tx.gateway_response['refund_response'] = dict(stripe_refund)
            else:
                raise ValueError(f"Unsupported gateway for refund: {gateway}")
            tx.save(update_fields=["status", "gateway_response"])
            if tx.booking:
                tx.booking.status = BookingStatus.CANCELLED
                tx.booking.save(update_fields=["status"])
            wallet, _ = Wallet.objects.get_or_create(user=tx.user)
            wallet.balance -= refund.amount
            wallet.save(update_fields=["balance", "updated_at"])
