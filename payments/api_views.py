from django.conf import settings  # type: ignore
from rest_framework import viewsets, status  # type: ignore
from rest_framework.decorators import action, api_view  # type: ignore
from rest_framework.response import Response  # type: ignore
from django.utils import timezone  # type: ignore
from django.db import transaction  # type: ignore
import uuid
import shortuuid  # type: ignore
from decimal import Decimal
import re
from django.db import transaction as db_transaction  # type: ignore
from rest_framework.views import APIView  # type: ignore


import requests
import base64
import zlib
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes
from cryptography import x509
from cryptography.hazmat.backends import default_backend

from bookings.models import BookingStatus
from .models import PaymentMethod, PaymentMethodType, PaymentTransaction, Wallet, Refund, PaymentStatus
from .serializers import PaymentMethodSerializer, PaymentTransactionSerializer, WalletSerializer, RefundSerializer
from .permissions import IsAdmin
import logging
from rest_framework.permissions import AllowAny  # type: ignore
from .tasks import send_refund_notification_email  # type: ignore

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

        # Now safe: user.id is UUID
        return PaymentMethod.objects.filter(user=user)

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

    # paypal initiate trascation function
    @action(methods=['post'], detail=True, url_path='initiate')
    def initiate_transaction(self, request, pk=None):
        try:
            transaction = self.get_object()
            if transaction.status != PaymentStatus.PENDING:
                logger.warning(f"Transaction {pk} is not pending: {transaction.status}")
                return Response({"error": "Transaction is not pending"}, status=status.HTTP_400_BAD_REQUEST)

            # Use GBP directly (no conversion needed for PayPal)
            gbp_amount_str = f"{transaction.amount:.2f}"
            if transaction.amount <= 0:
                logger.error(f"Invalid GBP amount for {pk}: {gbp_amount_str} (currency: {transaction.currency or 'GBP'})")
                return Response({"error": "Transaction amount must be greater than zero"}, status=status.HTTP_400_BAD_REQUEST)

            # Ensure currency is GBP
            if transaction.currency != 'GBP':
                logger.warning(f"Transaction {pk} currency is {transaction.currency}, but PayPal initiation requires GBP. Skipping conversion.")
                return Response({"error": "Transaction currency must be GBP for PayPal"}, status=status.HTTP_400_BAD_REQUEST)

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
                        "currency_code": "GBP",
                        "value": gbp_amount_str
                    },
                    "description": f"Payment for booking {transaction.booking.id if transaction.booking else 'Wallet'}",
                    "custom_id": transaction.reference  # For fallback lookup
                }],
                "application_context": {
                    "return_url": f"{settings.PAYPAL_RETURN_URL}/{transaction.id}",
                    "cancel_url": f"{settings.PAYPAL_CANCEL_URL}/{transaction.id}?cancelled=true",
                    "brand_name": "Drop and Roll",  # Your app name
                    "landing_page": "BILLING",
                    "user_action": "PAY_NOW"
                }
            }
            response = requests.post(url, headers=headers, json=body)
            
            # Always log response for debugging
            logger.info(f"PayPal order request for tx {transaction.id}: Status {response.status_code}, Body: {response.text}")

            try:
                response.raise_for_status()
                order_data = response.json()
                
                # Update transaction
                metadata = transaction.metadata or {}
                if isinstance(metadata, str):
                    import json
                    metadata = json.loads(metadata)
                metadata.update({
                    'gateway': 'paypal',
                    'paypal_order_id': order_data['id'],
                    'gbp_amount': gbp_amount_str  # Track GBP amount
                })
                transaction.metadata = metadata
                transaction.gateway_response = order_data  # Store full response
                transaction.save(update_fields=['metadata', 'gateway_response'])
                
                logger.info(f"PayPal order {order_data['id']} created for tx {transaction.id}")
                
                # Extract approval URL
                approval_url = next((link['href'] for link in order_data.get('links', []) if link['rel'] == 'approve'), None)
                return Response({
                    "success": True, 
                    "approval_url": approval_url,
                    "order_id": order_data['id'],
                    "links": order_data.get('links', [])
                }, status=status.HTTP_200_OK)
                
            except requests.exceptions.HTTPError as e:
                error_detail = response.json() if response.text else str(e)
                logger.error(f"PayPal order creation failed for tx {pk}: {str(e)} - Detail: {error_detail}")
                return Response({
                    "error": "Failed to initiate PayPal order", 
                    "detail": error_detail
                }, status=status.HTTP_400_BAD_REQUEST)  # 400 for client errors like invalid amount

        except Exception as e:
            logger.error(f"Unexpected error in initiate_transaction for {pk}: {str(e)}")
            return Response({"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

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

    @action(methods=['post'], detail=True, url_path='capture')
    def capture_transaction(self, request, pk=None):
        transaction = self.get_object()
        if transaction.status != PaymentStatus.PENDING:
            logger.warning(
                f"Transaction {pk} is not pending: {transaction.status}")
            return Response({"success": False, "error": "Transaction is not pending"}, status=status.HTTP_400_BAD_REQUEST)

        metadata = transaction.metadata
        if isinstance(metadata, str):
            import json
            metadata = json.loads(metadata)
        order_id = metadata.get('paypal_order_id')
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
            # Extract actual capture ID from nested structure
            captures = capture_data.get('purchase_units', [{}])[
                0].get('payments', {}).get('captures', [])
            if not captures:
                logger.error(
                    f"No captures found in response for transaction {pk}")
                return Response({"success": False, "error": "No capture in response"}, status=status.HTTP_400_BAD_REQUEST)
            # First (and usually only) capture
            capture_id = captures[0]['id']

            # Ensure gateway in metadata and set capture_id
            metadata.update({
                'gateway': 'paypal',
                'capture_id': capture_id
            })
            transaction.metadata = metadata
            transaction.status = PaymentStatus.SUCCESS
            transaction.gateway_response = dict(capture_data)
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
        except requests.exceptions.HTTPError as e:
            logger.error(
                f"PayPal capture failed for transaction {pk}: {str(e)} - Response: {response.text}")
            if not transaction.gateway_response:
                transaction.gateway_response = {}
            transaction.gateway_response['error'] = response.text
            transaction.status = PaymentStatus.FAILED
            transaction.save(update_fields=["status", "gateway_response"])
            return Response({"success": False, "error": "Capture failed"}, status=e.response.status_code)
        except requests.exceptions.RequestException as e:
            logger.error(
                f"PayPal capture request failed for transaction {pk}: {str(e)}")
            if not transaction.gateway_response:
                transaction.gateway_response = {}
            transaction.gateway_response['error'] = str(e)
            transaction.status = PaymentStatus.FAILED
            transaction.save(update_fields=["status", "gateway_response"])
            return Response({"success": False, "error": "Capture request failed"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            logger.error(
                f"Unexpected error capturing transaction {pk}: {str(e)}")
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

    def _extract_order_id(self, resource):
        """Helper: Extract order_id from resource.links 'up' rel."""
        links = resource.get('links', [])
        up_link = next(
            (link for link in links if link.get('rel') == 'up'), None)
        if up_link:
            href = up_link.get('href', '')
            # Parse /v2/checkout/orders/{order_id} – flexible regex
            match = re.search(r'/orders/([A-Z0-9]{10,20})', href)
            if match:
                return match.group(1)
        # Fallback (rare): supplementary_data
        return resource.get('supplementary_data', {}).get('related_ids', {}).get('order_id')

    def post(self, request):
        if not verify_webhook_signature(request, settings.PAYPAL_WEBHOOK_ID):
            logger.error("PayPal webhook signature verification failed")
            return Response({"error": "Invalid signature"}, status=status.HTTP_403_FORBIDDEN)

        data = request.data
        event_type = data.get('event_type')
        resource = data.get('resource')
        logger.debug(
            f"Received PayPal webhook event: {event_type}, resource ID: {resource.get('id') if resource else 'None'}")

        with db_transaction.atomic():
            try:
                if event_type == 'PAYMENT.CAPTURE.COMPLETED':
                    order_id = self._extract_order_id(resource)
                    if not order_id:
                        logger.error(
                            f"No order_id found in webhook resource links or supplementary_data")
                        return Response({"error": "No order_id in payload"}, status=status.HTTP_400_BAD_REQUEST)

                    tx = PaymentTransaction.objects.filter(
                        metadata__paypal_order_id=order_id).first()
                    if not tx:
                        # Fallback to custom_id (if present, though rare in capture resource)
                        custom_id = resource.get('custom_id')
                        if custom_id:
                            tx = PaymentTransaction.objects.filter(
                                reference=custom_id).first()
                    if not tx:
                        logger.error(
                            f"Transaction not found for order_id {order_id}")
                        return Response({"error": "Transaction not found"}, status=status.HTTP_400_BAD_REQUEST)

                    if tx.status != PaymentStatus.PENDING:
                        logger.warning(
                            f"Transaction {tx.id} already processed: {tx.status}")
                        return Response({"message": "Already processed"}, status=status.HTTP_200_OK)

                    # Update tx
                    tx.status = PaymentStatus.SUCCESS
                    tx.gateway_response = dict(resource)
                    if isinstance(tx.metadata, str):
                        import json
                        tx.metadata = json.loads(tx.metadata)
                    tx.metadata.update({
                        'gateway': 'paypal',
                        'capture_id': resource['id']
                    })
                    tx.save(update_fields=[
                            "status", "gateway_response", "metadata"])

                    # Update booking if present
                    if tx.booking:
                        tx.booking.status = BookingStatus.SCHEDULED
                        tx.booking.tracking_number = f"BK-{shortuuid.uuid()[:8].upper()}"
                        tx.booking.save(
                            update_fields=["status", "tracking_number"])
                        logger.info(
                            f"Webhook updated tx {tx.id} to SUCCESS, booking {tx.booking.id} to SCHEDULED (tracking: {tx.booking.tracking_number})")

                elif event_type == 'PAYMENT.CAPTURE.DENIED':
                    order_id = self._extract_order_id(resource)
                    if not order_id:
                        logger.warning(
                            "No order_id for DENIED event; skipping detailed update")
                        return Response({"message": "Webhook processed"}, status=status.HTTP_200_OK)

                    tx = PaymentTransaction.objects.filter(
                        metadata__paypal_order_id=order_id).first()
                    if tx and tx.status == PaymentStatus.PENDING:
                        tx.status = PaymentStatus.FAILED
                        tx.gateway_response = dict(resource)
                        tx.save(update_fields=["status", "gateway_response"])
                        logger.info(
                            f"Webhook set tx {tx.id} to FAILED (DENIED)")

                elif event_type == 'PAYMENT.CAPTURE.REFUNDED':
                    order_id = self._extract_order_id(resource)
                    if not order_id:
                        logger.error(f"No order_id for REFUNDED event")
                        return Response({"error": "No order_id in payload"}, status=status.HTTP_400_BAD_REQUEST)

                    tx = PaymentTransaction.objects.filter(
                        metadata__paypal_order_id=order_id).first()
                    if not tx:
                        logger.error(
                            f"Transaction not found for refunded order_id {order_id}")
                        return Response({"error": "Transaction not found"}, status=status.HTTP_400_BAD_REQUEST)

                    if tx.status not in [PaymentStatus.PENDING, PaymentStatus.SUCCESS]:
                        logger.warning(
                            f"Webhook refund for tx {tx.id} ignored (status: {tx.status})")
                        return Response({"message": "Invalid status for refund"}, status=status.HTTP_200_OK)

                    # Create Refund record for audit (full amount) – this triggers post_save signal for email
                    refund, created = Refund.objects.get_or_create(
                        transaction=tx,
                        defaults={
                            'amount': tx.amount,
                            'reason': 'Auto-refund via PayPal webhook',
                            'status': 'processed',  # Assume success from webhook
                            'gateway_response': dict(resource),
                            'admin_user': None,  # Webhook, no admin
                        }
                    )
                    if not created:
                        logger.warning(
                            f"Duplicate Refund record for tx {tx.id}; skipping updates")
                        return Response({"message": "Duplicate refund ignored"}, status=status.HTTP_200_OK)

                    # Proceed with updates (now safe)
                    tx.status = PaymentStatus.REFUNDED
                    tx.gateway_response = dict(resource)
                    if isinstance(tx.metadata, str):
                        import json
                        tx.metadata = json.loads(tx.metadata)
                    tx.metadata.update({
                        'gateway': 'paypal',
                        'refund_id': resource['id']  # Store for reference
                    })
                    tx.save(update_fields=[
                            "status", "gateway_response", "metadata"])

                    # Update booking
                    if tx.booking:
                        tx.booking.status = BookingStatus.REFUNDED
                        tx.booking.save(update_fields=["status"])

                    logger.info(
                        f"Webhook processed refund for tx {tx.id}; set to REFUNDED")

                else:
                    logger.debug(f"Ignored webhook event: {event_type}")
                    return Response({"message": "Event ignored"}, status=status.HTTP_200_OK)

                return Response({"message": "Webhook processed"}, status=status.HTTP_200_OK)

            except Exception as e:
                logger.error(
                    f"Error processing webhook {event_type}: {str(e)}")
                return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


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
    queryset = Refund.objects.select_related(
        'transaction__booking', 'admin_user').all()
    serializer_class = RefundSerializer
    permission_classes = [IsAdmin]

    def get_queryset(self):
        user = self.request.user
        if user.is_anonymous:
            return Refund.objects.none()
        return Refund.objects.filter(
            transaction__user=user
        ) | Refund.objects.filter(
            transaction__guest_email__iexact=user.email
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context.update({"request": self.request})
        return context

    def perform_create(self, serializer):
        # Saves with admin_user, triggers serializer.create (which calls process_refund)
        refund = serializer.save(admin_user=self.request.user)
