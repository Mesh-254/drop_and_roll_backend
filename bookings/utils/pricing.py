from decimal import Decimal
from django.core.cache import cache
from django.core.exceptions import ValidationError
from ..models import ShippingType, ServiceType, PricingRule


def _load_pricing_rules() -> dict[str, Decimal]:
    rules = cache.get("pricing_rules")
    if rules is None:
        rules = {r.key: r.value for r in PricingRule.objects.all()}
        cache.set("pricing_rules", rules, timeout=3600)
    return rules


def get_weight_tier(weight_kg: Decimal) -> int | None:
    if weight_kg <= Decimal("5"):
        return 5
    if weight_kg <= Decimal("10"):
        return 10
    if weight_kg <= Decimal("15"):
        return 15
    if weight_kg <= Decimal("20"):
        return 20
    if weight_kg <= Decimal("30"):
        return 30
    return None


def compute_quote(
    shipment_type: str,
    service_type: str,
    weight_kg: Decimal,
    distance_km: Decimal,
    num_parcels: int = 1,
    insurance_amount: Decimal = Decimal("0"),
    discount: Decimal = Decimal("0"),
    dimensions: dict | None = None,
    fragile: bool = False,  # kept for signature compatibility – ignored
) -> tuple[Decimal, Decimal, dict]:
    dimensions = dimensions or {}

    pricing_rules = _load_pricing_rules()
    service_types = cache.get("service_types") or {
        st.name: st for st in ServiceType.objects.all()
    }

    # Validation
    max_weight = pricing_rules.get("MAX_WEIGHT_KG", Decimal("50"))
    max_distance = pricing_rules.get("MAX_DISTANCE_KM", Decimal("500"))

    if weight_kg > max_weight:
        raise ValidationError(f"Maximum weight allowed is {max_weight} kg")
    if distance_km > max_distance:
        raise ValidationError(f"Maximum distance allowed is {max_distance} km")
    if num_parcels < 1:
        raise ValidationError("At least 1 parcel required")

    tier = get_weight_tier(weight_kg)
    if tier is None:
        raise ValidationError(
            f"Weight {weight_kg} kg exceeds maximum supported tier (30 kg)"
        )

    # ─── Load tier-specific values ───────────────────────────────────────
    base_price_key = f"BASE_{tier}KG"
    extra_parcel_key = f"EXTRA_PARCEL_{tier}KG"

    base_price = pricing_rules.get(base_price_key, Decimal("12.00"))
    extra_parcel_charge = pricing_rules.get(extra_parcel_key, Decimal("4.00"))
    base_distance_km = pricing_rules.get("BASE_DISTANCE_KM", Decimal("25.00"))
    extra_km_charge = pricing_rules.get("EXTRA_KM_CHARGE", Decimal("0.80"))
    insurance_rate = pricing_rules.get("INSURANCE_RATE", Decimal("0.02"))

    # ─── Core calculation ─────────────────────────────────────────────────
    extra_km = max(Decimal(0), distance_km - base_distance_km)
    extra_distance = extra_km * extra_km_charge

    extra_parcels = max(0, num_parcels - 1)
    extra_parcel_fee = Decimal(extra_parcels) * extra_parcel_charge

    tier_subtotal = base_price + extra_distance + extra_parcel_fee

    # ─── Apply service type adjustments ──────────────────────────────
    service = service_types.get(service_type)
    if not service:
        raise ValidationError(f"Service type '{service_type}' not found")

    service_subtotal = tier_subtotal * service.urgency_multiplier

    # Enforce minimum price
    if service.minimum_price > service_subtotal:
        service_subtotal = service.minimum_price

    # Insurance
    insurance_fee = (
        insurance_amount * insurance_rate if insurance_amount > 0 else Decimal("0")
    )

    total_before_discount = service_subtotal + insurance_fee

    final_price = max(total_before_discount - discount, Decimal("0"))

    # ─── Detailed breakdown (shown in quote meta / receipt) ───────
    breakdown = {
        "tier": f"up to {tier} kg",
        "num_parcels": num_parcels,
        "tier_base": float(base_price),
        "extra_distance_km": float(extra_km),
        "extra_distance_charge": float(extra_distance),
        "extra_parcels": extra_parcels,
        "extra_parcel_charge_per": float(extra_parcel_charge),
        "extra_parcel_fee": float(extra_parcel_fee),
        "tier_subtotal": float(tier_subtotal),
        "service_multiplier": float(service.urgency_multiplier),
        "service_minimum_applied": service.minimum_price > tier_subtotal,
        "service_adjusted_subtotal": float(service_subtotal),
        "insurance_fee": float(insurance_fee),
        "discount": float(discount),
        "final_price": float(final_price),
        "used_rules": {
            "base": base_price_key,
            "extra_parcel": extra_parcel_key,
        },
    }

    return tier_subtotal, final_price, breakdown
