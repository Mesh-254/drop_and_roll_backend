from decimal import Decimal
from django.core.exceptions import ValidationError
from ..models import ServiceTier, ShipmentType

def compute_quote(
    shipment_type: str,
    service_tier: str,
    weight_kg: Decimal,
    distance_km: Decimal,
    fragile: bool = False,
    insurance_amount: Decimal = Decimal('0'),
    dimensions: dict = None,
    surge: Decimal = Decimal('1'),
    discount: Decimal = Decimal('0')
) -> tuple[Decimal, Decimal, dict]:
    """
    Compute a quote for a shipment based on provided parameters.
    
    Args:
        shipment_type: Type of shipment (e.g., 'parcels', 'cargo').
        service_tier: Service level (e.g., 'standard', 'express').
        weight_kg: Weight of the shipment in kilograms.
        distance_km: Distance of the shipment in kilometers.
        fragile: Whether the shipment is fragile (adds 25% to subtotal).
        insurance_amount: Insured value of the shipment (2% fee if > 0).
        dimensions: Dict with width, length, height, unit (e.g., {'width': 10, 'length': 20, 'height': 30, 'unit': 'cm'}).
        surge: Surge multiplier (default 1.0).
        discount: Discount amount to subtract from final price (default 0).
    
    Returns:
        Tuple containing (base_price, final_price, breakdown_dict).
    
    Raises:
        ValidationError: If shipment_type, service_tier, weight_kg, or distance_km are invalid.
    """
    # Validate inputs
    if shipment_type not in ShipmentType.values:
        raise ValidationError(f"Invalid shipment_type: {shipment_type}")
    if service_tier not in ServiceTier.values:
        raise ValidationError(f"Invalid service_tier: {service_tier}")
    if weight_kg < 0:
        raise ValidationError("Weight cannot be negative")
    if distance_km < 0:
        raise ValidationError("Distance cannot be negative")
    if insurance_amount < 0:
        raise ValidationError("Insurance amount cannot be negative")
    if surge < 0:
        raise ValidationError("Surge multiplier cannot be negative")
    if discount < 0:
        raise ValidationError("Discount cannot be negative")
    dimensions = dimensions or {}

    # Define base prices for each service tier
    base_prices = {
        ServiceTier.STANDARD: Decimal('8.00'),
        ServiceTier.EXPRESS: Decimal('25.00'),
        ServiceTier.BUSINESS: Decimal('12.00'),
        ServiceTier.SPECIALIZED: Decimal('30.00'),
    }

    # Calculate base components
    base_price = base_prices.get(service_tier, Decimal('0'))
    weight_charge = weight_kg * Decimal('0.50')  # $0.50 per kg
    distance_charge = distance_km * Decimal('0.10')  # $0.10 per km

    # Compute subtotal
    subtotal = base_price + weight_charge + distance_charge

    # Apply additional fees
    insurance_fee = insurance_amount * Decimal('0.02') if insurance_amount > 0 else Decimal('0')
    fragile_charge = subtotal * Decimal('0.25') if fragile else Decimal('0')

    # Compute total before surge and discount
    total_price = subtotal + insurance_fee + fragile_charge

    # Apply surge and discount
    final_price = total_price * surge - discount

    # Ensure final price is non-negative
    final_price = max(final_price, Decimal('0'))

    # Build breakdown dictionary for audit trail, converting Decimals to float
    breakdown = {
        'shipment_type': shipment_type,
        'service_tier': service_tier,
        'base_price': float(base_price),
        'weight_charge': float(weight_charge),
        'distance_charge': float(distance_charge),
        'subtotal': float(subtotal),
        'insurance_fee': float(insurance_fee),
        'fragile_charge': float(fragile_charge),
        'surge_multiplier': float(surge),
        'discount': float(discount),
        'dimensions': dimensions,
    }

    return total_price, final_price, breakdown