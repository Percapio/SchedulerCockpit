import pytest
import math
from datetime import date, timedelta
from cockpit.services.runtime_calc import (
    RuntimeInputs, RuntimeResults, compute, compute_tht, compute_aoi, compute_shipping
)
from cockpit.services.runtime_constants import RuntimeConstants
from cockpit.services.audit_read import start_by
from cockpit.persistence.schema import migrate_to_v11

def test_compute_tht():
    constants = RuntimeConstants.defaults()
    # Baseline
    inputs = RuntimeInputs(
        smt_placements=100, smt_unique_mpns=10, tht_placements=50, quantity=100, sides=1,
        is_class_3=False, is_clean_process=False
    )
    tht_base = compute_tht(inputs, constants)
    expected_base = 50 * 100 * 0.15 / 60
    assert math.isclose(tht_base, expected_base)

    # Both flags (multiplicative compounding)
    inputs_both = RuntimeInputs(
        smt_placements=100, smt_unique_mpns=10, tht_placements=50, quantity=100, sides=1,
        is_class_3=True, is_clean_process=True
    )
    tht_both = compute_tht(inputs_both, constants)
    assert math.isclose(tht_both, expected_base * 1.2 * 1.1)

def test_compute_aoi():
    constants = RuntimeConstants.defaults()
    inputs = RuntimeInputs(
        smt_placements=100, smt_unique_mpns=10, tht_placements=50, quantity=100, sides=3,
        is_class_3=False, is_clean_process=False
    )
    aoi_base = compute_aoi(inputs, constants)
    # sides clamped to 2
    # inspection_hours = 100 * 2 * 100 * 0.0004 = 8.0
    # math.ceil(8.0) = 8
    assert aoi_base == 8.0
    
    # Negative sides clamped to 1
    inputs.sides = -1
    aoi_neg = compute_aoi(inputs, constants)
    # 100 * 1 * 100 * 0.0004 = 4.0
    assert aoi_neg == 4.0

    # Class 3 multiplier
    inputs.is_class_3 = True
    aoi_class_3 = compute_aoi(inputs, constants)
    # ceil(4.0 * 1.2) = ceil(4.8) = 5
    assert aoi_class_3 == 5.0

def test_compute_shipping():
    constants = RuntimeConstants.defaults()
    inputs = RuntimeInputs(
        smt_placements=100, smt_unique_mpns=10, tht_placements=50, quantity=105, sides=1,
    )
    shipping = compute_shipping(inputs, constants)
    # 2.0 + ceil(105 / 100) = 2.0 + 2 = 4.0
    assert shipping == 4.0
    
    # Quantity 0
    inputs.quantity = 0
    shipping_zero = compute_shipping(inputs, constants)
    assert shipping_zero == 2.0

    # Zero rate guard
    zero_rate_constants = RuntimeConstants(shipping_boards_per_hour=0.0)
    shipping_zero_rate = compute_shipping(inputs, zero_rate_constants)
    assert shipping_zero_rate == 2.0
    
def test_compute_all():
    constants = RuntimeConstants.defaults()
    inputs = RuntimeInputs(
        smt_placements=100, smt_unique_mpns=10, tht_placements=50, quantity=105, sides=1,
    )
    results = compute(inputs, constants)
    assert results.feeder == 10 / 30
    assert results.tht == compute_tht(inputs, constants)
    assert results.aoi == compute_aoi(inputs, constants)
    assert results.shipping == compute_shipping(inputs, constants)
    assert results.ops == 0.0
    
def test_start_by():
    d = date(2026, 7, 24) # Friday
    holidays = set()
    # 3 days needed -> Thur, Wed, Tue
    assert start_by(d, 24.0, holidays) == date(2026, 7, 21)
