"""Phase 48 sections 8.1 and 8.2 -- the DigiKey parameter mapping.

Making the duplicate case a test failure rather than a runtime rule is the
point: two labels for one quantity is drift, and it should be caught when the
mapping is edited, not resolved silently by iteration order at runtime.
"""

from cockpit.services.mpn_library.digikey_variant import PARAM_MAPPING, extract_variants


def _product(parameters: dict) -> dict:
    return {
        "Products": [{
            "DigiKeyPartNumber": "DK-1",
            "Manufacturer": {"Value": "Yageo"},
            "Packaging": {"Value": "Tape & Reel (TR)"},
            "Parameters": [
                {"Parameter": label, "Value": value}
                for label, value in parameters.items()
            ],
        }]
    }


def test_no_attribute_name_appears_twice_for_reel_quantity():
    """Fails against the pre-Phase-48 mapping, where Standard Package and
    Quantity per Reel both projected onto quantity_per_reel."""
    candidates = extract_variants("RC0805", _product({
        "Quantity per Reel": "5000",
        "Standard Package": "1",
        "Package / Case": "0805",
    }))

    assert len(candidates) == 1
    names = [attribute.attribute_name for attribute in candidates[0].attributes]
    assert len(names) == len(set(names))
    assert names.count("quantity_per_reel") == 1


def test_standard_package_maps_to_nothing():
    """It is DigiKey's order multiple, not the quantity on a reel, and for a
    cut-tape variant the two differ by orders of magnitude."""
    assert "Standard Package" not in PARAM_MAPPING

    candidates = extract_variants("RC0805", _product({
        "Quantity per Reel": "5000",
        "Standard Package": "1",
    }))
    quantity = next(
        attribute for attribute in candidates[0].attributes
        if attribute.attribute_name == "quantity_per_reel"
    )
    assert quantity.value_text == "5000"
    assert quantity.source_label == "Quantity per Reel"


def test_standard_package_alone_supplies_no_reel_quantity():
    candidates = extract_variants("RC0805", _product({
        "Standard Package": "1",
        "Tape Width": "8mm",
    }))
    names = [attribute.attribute_name for attribute in candidates[0].attributes]
    assert "quantity_per_reel" not in names


def test_no_mapping_produces_a_duplicate_registry_name():
    """The whole table, not just the reel quantity: one registry name may be
    supplied by several labels only if no single record can carry two."""
    reverse: dict[str, list[str]] = {}
    for label, registry_name in PARAM_MAPPING.items():
        reverse.setdefault(registry_name, []).append(label)

    # Tape/Carrier pairs are DigiKey's two spellings for one parameter and
    # never both appear on one record. Everything else must be unique.
    known_synonyms = {
        "carrier_width_mm": {"Tape Width", "Carrier Width"},
        "carrier_pitch_mm": {"Tape Pitch", "Carrier Pitch"},
    }
    for registry_name, labels in reverse.items():
        if len(labels) == 1:
            continue
        assert set(labels) == known_synonyms.get(registry_name), (
            f"{registry_name} is supplied by {labels}; add it to the synonym "
            f"set only if no single DigiKey record can carry both."
        )


def test_known_gaps_stay_unmapped():
    """The Phase 48 snapshot survey found no retained response to confirm a
    label against, so these three registry names are recorded as known gaps.
    A speculative mapping entry is invisible: the next reader cannot tell an
    unmapped name from an unchecked one. See scripts/survey_parameter_labels.py."""
    for registry_name in ("pin_count", "body_length_mm", "body_width_mm"):
        assert registry_name not in PARAM_MAPPING.values()


def test_pin_one_orientation_has_no_digikey_source():
    """Required for SMT programming, supplied by nobody. Operator-entered."""
    assert "pin_one_orientation" not in PARAM_MAPPING.values()
