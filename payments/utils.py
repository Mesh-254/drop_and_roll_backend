# payments/utils.py
import uuid
from decimal import Decimal
from django.db import transaction as db_transaction
from django.utils import timezone
from rest_framework import serializers
import stripe
import requests
from django.conf import settings
from .models import PaymentStatus, Wallet, Refund
from bookings.models import BookingStatus
import logging
import json  # For metadata handling

logger = logging.getLogger(__name__)
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
# payments/utils.py - Update process_refund function


def process_refund(refund_instance, admin_user=None, idempotency_key=None):
    """Process full refund: Gateway, updates, notifications. Returns True on success."""
    with db_transaction.atomic():
        tx = refund_instance.transaction
        metadata = tx.metadata or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)

        gateway = None
        if 'capture_id' in metadata or 'paypal_order_id' in metadata:
            gateway = 'paypal'
        elif 'stripe_payment_intent_id' in metadata:
            gateway = 'stripe'

        if not gateway:
            raise serializers.ValidationError(
                f"Unsupported/unknown gateway for transaction {tx.reference}. Missing metadata keys.")

        refund_instance.status = 'pending'
        refund_instance.save(update_fields=['status'])
        if not idempotency_key:
            idempotency_key = str(uuid.uuid4())

        try:
            refund_response = {}
            if gateway == 'paypal':
                capture_id = metadata.get('capture_id')
                if not capture_id:
                    raise ValueError(
                        "No valid capture ID found in transaction metadata")

                access_token = get_access_token()
                url = f"{settings.PAYPAL_API_URL}/v2/payments/captures/{capture_id}/refund"
                api_headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {access_token}",
                    "PayPal-Request-Id": idempotency_key,
                }
                body = {}  # Full refund
                response = requests.post(url, headers=api_headers, json=body)

                # Log full response for debugging
                logger.info(
                    f"PayPal refund API for capture {capture_id}: Status {response.status_code}, Body: {response.text}")

                try:
                    response.raise_for_status()
                except requests.exceptions.HTTPError as e:
                    if response.status_code == 422:
                        try:
                            error_data = response.json()
                            # FIX: Check nested 'details' array
                            details = error_data.get('details', [])
                            is_already_refunded = False
                            specific_issue = "Unknown"
                            specific_description = error_data.get(
                                'message', 'Unknown error')

                            for detail in details:
                                issue = detail.get('issue', '')
                                description = detail.get('description', '')
                                if any(keyword in (issue + description).lower() for keyword in [
                                    'already refunded', 'refund already completed',
                                    'ineligible capture', 'fully refunded', 'capture fully refunded'
                                ]):
                                    is_already_refunded = True
                                    specific_issue = issue
                                    specific_description = description
                                    break

                            if is_already_refunded:
                                logger.info(
                                    f"PayPal refund already processed for tx {tx.id}: {specific_description} (Issue: {specific_issue})")
                                refund_instance.status = 'processed'
                                if not refund_instance.gateway_response:
                                    refund_instance.gateway_response = {}
                                refund_instance.gateway_response = {
                                    'error': 'Already refunded', 'details': error_data}
                                refund_instance.admin_user = admin_user
                                refund_instance.save(
                                    update_fields=['status', 'gateway_response', 'admin_user'])
                                # Skip further updates (idempotency)
                                return True
                            else:
                                # Raise with specific detail
                                raise serializers.ValidationError(
                                    f"Refund ineligible: {specific_description} (Code: {specific_issue})")
                        except json.JSONDecodeError:
                            raise serializers.ValidationError(
                                f"Refund failed: {response.text}")
                    raise  # Other errors

                # Rest of success handling remains the same...
                refund_response = response.json()
                if not tx.gateway_response:
                    tx.gateway_response = {}
                tx.gateway_response['refund_response'] = refund_response
                tx.save(update_fields=['gateway_response'])
                refund_instance.gateway_response = refund_response
                refund_instance.save(update_fields=['gateway_response'])

            elif gateway == 'stripe':
                # Existing Stripe logic unchanged...

                # Common success updates (after gateway-specific code)...
                tx.status = PaymentStatus.REFUNDED
                tx.save(update_fields=['status'])

            if tx.booking:
                if tx.booking.status == BookingStatus.DELIVERED:
                    tx.booking.status = BookingStatus.REFUNDED
                else:
                    tx.booking.status = BookingStatus.CANCELLED
                tx.booking.save(update_fields=['status'])

            if tx.user:
                wallet, _ = Wallet.objects.get_or_create(user=tx.user)
                wallet.balance += tx.amount
                wallet.updated_at = timezone.now()
                wallet.save(update_fields=['balance', 'updated_at'])

            refund_instance.status = 'processed'
            refund_instance.admin_user = admin_user
            refund_instance.save(update_fields=['status', 'admin_user'])

            logger.info(
                f"Refund {refund_instance.id} processed for {tx.reference} via {gateway}.")
            return True

        except Exception as e:
            refund_instance.status = 'failed'
            refund_instance.save(update_fields=['status'])
            if not tx.gateway_response:
                tx.gateway_response = {}
            tx.gateway_response['refund_error'] = str(e)
            tx.save(update_fields=['gateway_response'])
            logger.error(f"Refund {refund_instance.id} failed: {e}")
            raise
