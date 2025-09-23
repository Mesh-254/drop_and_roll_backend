# business/utils/pricing.py
from decimal import Decimal
from ..models import BusinessPricing  # Relative import


def compute_business_quote(shipping_type, service_type, weight_kg, distance_km, fragile, insurance_amount, dimensions,
                           surge=1, discount=0):
    """
    Compute business-specific pricing based on admin-defined rates.
    Returns (base_price, final_price, breakdown).
    """
    try:
        pricing = BusinessPricing.objects.get(shipping_type=shipping_type, service_type=service_type)
    except BusinessPricing.DoesNotExist:
        raise ValueError("No pricing defined for this shipping and service type combination")

    # Calculate base price
    weight_cost = pricing.base_price_per_kg * Decimal(str(weight_kg))
    distance_cost = pricing.base_price_per_km * Decimal(str(distance_km))
    base_price = weight_cost + distance_cost

    # Add fragile surcharge
    fragile_cost = pricing.fragile_surcharge if fragile else Decimal('0')

    # Add insurance cost
    insurance_cost = Decimal(str(insurance_amount)) * (
                pricing.insurance_rate / Decimal('100')) if insurance_amount else Decimal('0')

    # Apply surge and discount
    subtotal = base_price + fragile_cost + insurance_cost
    surge_amount = subtotal * (Decimal(str(surge)) - Decimal('1'))
    final_price = subtotal + surge_amount - Decimal(str(discount))

    # Ensure final price is non-negative
    final_price = max(final_price, Decimal('0'))

    breakdown = {
        'weight_cost': float(weight_cost),
        'distance_cost': float(distance_cost),
        'fragile_cost': float(fragile_cost),
        'insurance_cost': float(insurance_cost),
        'surge_amount': float(surge_amount),
        'discount_amount': float(discount),
    }

    return base_price, final_price, breakdown