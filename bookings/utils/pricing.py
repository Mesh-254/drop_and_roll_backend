from decimal import Decimal
from django.core.exceptions import ValidationError
from django.core.cache import cache
from ..models import ShippingType, ServiceType


def compute_quote(
    shipment_type: str,
    service_type: str,
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
        shipment_type: Name of the shipping type (e.g., 'Parcels', 'Cargo').
        service_type: Name of the service type (e.g., 'Standard', 'Express').
        weight_kg: Weight of the shipment in kilograms.
        distance_km: Distance of the shipment in kilometers.
        fragile: Whether the shipment is fragile (adds 25% to subtotal).
        insurance_amount: Insured value of the shipment (2% fee if > 0).
        dimensions: Dict with width, length, height, unit (e.g., {'width': 10, 'length': 20, 'height': 30, 'unit': 'cm'}).
        surge: Surge multiplier (default 1.0).
        discount: Discount amount to subtract from final price (default 0).
    
    Returns:
        Tuple containing (subtotal, final_price, breakdown_dict).
    
    Raises:
        ValidationError: If inputs are invalid.
    """
    # Validate inputs
    dimensions = dimensions or {}
    if not isinstance(dimensions, dict):
        raise ValidationError("Dimensions must be a dictionary")

    # Cache shipping and service types for 1 hour
    shipping_types = cache.get('shipping_types')
    if not shipping_types:
        shipping_types = {st.name: st for st in ShippingType.objects.all()}
        cache.set('shipping_types', shipping_types, 3600)
    
    service_types = cache.get('service_types')
    if not service_types:
        service_types = {st.name: st for st in ServiceType.objects.all()}
        cache.set('service_types', service_types, 3600)

    if shipment_type not in shipping_types:
        raise ValidationError(f"Invalid shipment_type: {shipment_type}")
    if service_type not in service_types:
        raise ValidationError(f"Invalid service_type: {service_type}")
    if weight_kg < 0 or weight_kg > 1000:  # Realistic max weight
        raise ValidationError("Weight must be between 0 and 1000 kg")
    if distance_km < 0 or distance_km > 10000:  # Realistic max distance
        raise ValidationError("Distance must be between 0 and 10000 km")
    if insurance_amount < 0:
        raise ValidationError("Insurance amount cannot be negative")
    if surge < 0:
        raise ValidationError("Surge multiplier cannot be negative")
    if discount < 0:
        raise ValidationError("Discount cannot be negative")

    # Calculate base components
    service_type = service_types[service_type]
    base_price = service_type.price  # Use dynamic price from model
    weight_charge = weight_kg * Decimal('0.50')  # $0.50 per kg
    distance_charge = distance_km * Decimal('0.10')  # $0.10 per km

    # Compute subtotal
    base_price = base_price + weight_charge + distance_charge

    # Apply additional fees
    insurance_fee = insurance_amount * Decimal('0.02') if insurance_amount > 0 else Decimal('0')
    fragile_charge = base_price * Decimal('0.25') if fragile else Decimal('0')

    # Compute total before surge and discount
    total_price = base_price + insurance_fee + fragile_charge

    # Apply surge and discount
    final_price = total_price * surge - discount

    # Ensure final price is non-negative
    final_price = max(final_price, Decimal('0'))

    # Build breakdown dictionary for audit trail, converting Decimals to float
    breakdown = {
        'shipment_type': shipment_type,
        'service_type': service_type,
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

    return subtotal, final_price, breakdown