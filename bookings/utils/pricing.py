from decimal import Decimal

BASES = {
    "standard": Decimal("150.00"),
    "express": Decimal("250.00"),
    "business": Decimal("120.00"),
}
PER_KM = {
    "standard": Decimal("35.00"),
    "express": Decimal("50.00"),
    "business": Decimal("30.00"),
}
PER_KG = Decimal("20.00")


def compute_quote(*, service_tier: str, weight_kg: Decimal, distance_km: Decimal, surge: Decimal = Decimal("1.00"), discount: Decimal = Decimal("0.00")):
    base = BASES[service_tier]
    price = base + (PER_KM[service_tier] * distance_km) + (PER_KG * weight_kg)
    price = (price * surge).quantize(Decimal("0.01"))
    final = (price - discount).quantize(Decimal("0.01"))
    breakdown = {
        "base": str(base),
        "per_km": str(PER_KM[service_tier]),
        "per_kg": str(PER_KG),
        "surge": str(surge),
        "discount": str(discount),
        "computed_price_before_discount": str(price),
        "final": str(final),
    }
    return price, final, breakdown