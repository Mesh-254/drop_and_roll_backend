# bookings/utils/pricing.py   (replace the old file)

from decimal import Decimal
from django.core.cache import cache
from django.core.exceptions import ValidationError
from ..models import ShippingType, ServiceType, PricingRule


def _load_pricing_rules() -> dict[str, Decimal]:
    """
    Load *all* PricingRule rows into a dict {key: Decimal(value)}.
    Cached for 1 hour (same TTL you used for Shipping/Service types).
    """
    rules = cache.get("pricing_rules")
    if rules is None:
        rules = {
            r.key: r.value for r in PricingRule.objects.all()
        }
        cache.set("pricing_rules", rules, timeout=60 * 60)
    return rules


def compute_quote(
    shipment_type: str,
    service_type: str,
    weight_kg: Decimal,
    distance_km: Decimal,
    fragile: bool = False,
    insurance_amount: Decimal = Decimal("0"),
    dimensions: dict | None = None,
    surge: Decimal = Decimal("1"),
    discount: Decimal = Decimal("0"),
) -> tuple[Decimal, Decimal, dict]:
    """
    Same signature as before – **no hard-coded numbers any more**.
    """
    dimensions = dimensions or {}
    if not isinstance(dimensions, dict):
        raise ValidationError("Dimensions must be a dictionary")

    # ---------- 1. Load everything from cache ----------
    shipping_types = cache.get("shipping_types")
    if not shipping_types:
        shipping_types = {st.name: st for st in ShippingType.objects.all()}
        cache.set("shipping_types", shipping_types, 3600)

    service_types = cache.get("service_types")
    if not service_types:
        service_types = {st.name: st for st in ServiceType.objects.all()}
        cache.set("service_types", service_types, 3600)

    pricing_rules = _load_pricing_rules()

    # ---------- 2. Basic validation ----------
    if shipment_type not in shipping_types:
        raise ValidationError(f"Invalid shipment_type: {shipment_type}")
    if service_type not in service_types:
        raise ValidationError(f"Invalid service_type: {service_type}")

    # Pull limits from DB (fallback to very large numbers if missing)
    max_weight = pricing_rules.get("MAX_WEIGHT_KG", Decimal("1000"))
    max_distance = pricing_rules.get("MAX_DISTANCE_KM", Decimal("10000"))

    if not (Decimal("0") <= weight_kg <= max_weight):
        raise ValidationError(f"Weight must be 0–{max_weight} kg")
    if not (Decimal("0") <= distance_km <= max_distance):
        raise ValidationError(f"Distance must be 0–{max_distance} km")
    if insurance_amount < 0:
        raise ValidationError("Insurance amount cannot be negative")
    if surge < 0:
        raise ValidationError("Surge multiplier cannot be negative")
    if discount < 0:
        raise ValidationError("Discount cannot be negative")

    # ---------- 3. Core calculation ----------
    service = service_types[service_type]
    base_price = service.price                     # still comes from ServiceType

    weight_charge = weight_kg * pricing_rules.get("WEIGHT_PER_KG", Decimal("0.50"))
    distance_charge = distance_km * pricing_rules.get("DISTANCE_PER_KM", Decimal("0.10"))

    subtotal = base_price + weight_charge + distance_charge

    insurance_fee = (
        insurance_amount * pricing_rules.get("INSURANCE_RATE", Decimal("0.02"))
        if insurance_amount > 0
        else Decimal("0")
    )
    fragile_charge = (
        base_price * pricing_rules.get("FRAGILE_MULTIPLIER", Decimal("0.25"))
        if fragile
        else Decimal("0")
    )

    total_price = subtotal + insurance_fee + fragile_charge
    final_price = max(total_price * surge - discount, Decimal("0"))

    # ---------- 4. Breakdown ----------
    breakdown = {
        "shipment_type": shipment_type,
        "service_type": service_type,
        "base_price": float(base_price),
        "weight_charge": float(weight_charge),
        "distance_charge": float(distance_charge),
        "subtotal": float(subtotal),
        "final_price": float(final_price),
        "insurance_fee": float(insurance_fee),
        "fragile_charge": float(fragile_charge),
        "surge_multiplier": float(surge),
        "discount": float(discount),
        "dimensions": dimensions,
    }

    return subtotal, final_price, breakdown