from .types import AttributeSpec, AttributeGroup, ValueKind

ATTRIBUTE_REGISTRY = {
    "carrier_width_mm": AttributeSpec("carrier_width_mm", "Carrier Width", "mm", ValueKind.NUMERIC, AttributeGroup.CARRIER),
    "carrier_pitch_mm": AttributeSpec("carrier_pitch_mm", "Carrier Pitch", "mm", ValueKind.NUMERIC, AttributeGroup.CARRIER),
    "reel_diameter_mm": AttributeSpec("reel_diameter_mm", "Reel Diameter", "mm", ValueKind.NUMERIC, AttributeGroup.CARRIER),
    "quantity_per_reel": AttributeSpec("quantity_per_reel", "Quantity per Reel", "pcs", ValueKind.NUMERIC, AttributeGroup.CARRIER),
    "pin_one_orientation": AttributeSpec("pin_one_orientation", "Pin 1 Orientation", None, ValueKind.ENUM, AttributeGroup.CARRIER),
    "eia481_revision": AttributeSpec("eia481_revision", "EIA-481 Revision", None, ValueKind.TEXT, AttributeGroup.CARRIER),
    
    "package_case": AttributeSpec("package_case", "Package / Case", None, ValueKind.TEXT, AttributeGroup.PACKAGE),
    "package_code": AttributeSpec("package_code", "Package Code", None, ValueKind.TEXT, AttributeGroup.PACKAGE),
    "supplier_device_package": AttributeSpec("supplier_device_package", "Supplier Device Package", None, ValueKind.TEXT, AttributeGroup.PACKAGE),
    "mounting_type": AttributeSpec("mounting_type", "Mounting Type", None, ValueKind.ENUM, AttributeGroup.PACKAGE),
    "pin_count": AttributeSpec("pin_count", "Pin Count", None, ValueKind.NUMERIC, AttributeGroup.PACKAGE),
    
    "body_length_mm": AttributeSpec("body_length_mm", "Body Length", "mm", ValueKind.NUMERIC, AttributeGroup.BODY),
    "body_width_mm": AttributeSpec("body_width_mm", "Body Width", "mm", ValueKind.NUMERIC, AttributeGroup.BODY),
    "seated_height_mm": AttributeSpec("seated_height_mm", "Seated Height", "mm", ValueKind.NUMERIC, AttributeGroup.BODY),
    
    "moisture_sensitivity_level": AttributeSpec("moisture_sensitivity_level", "MSL", None, ValueKind.TEXT, AttributeGroup.HANDLING),
    "operating_temp_min_c": AttributeSpec("operating_temp_min_c", "Min Operating Temp", "°C", ValueKind.NUMERIC, AttributeGroup.HANDLING),
    "operating_temp_max_c": AttributeSpec("operating_temp_max_c", "Max Operating Temp", "°C", ValueKind.NUMERIC, AttributeGroup.HANDLING),
    "rohs_status": AttributeSpec("rohs_status", "RoHS Status", None, ValueKind.TEXT, AttributeGroup.HANDLING),
}
