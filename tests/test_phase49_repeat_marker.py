"""Phase 49 section 8.2 — derive_repeat_marker.

The equivalence test at the bottom is what holds derive_repeat_marker and the
retained derive_repeat together; nothing else stops the two derivations of the
same string from drifting.
"""

import pytest

from cockpit.services.repeat import RepeatMarker, derive_repeat, derive_repeat_marker


def test_new_assembly_carries_no_reference():
    marker = derive_repeat_marker({"assembly_type": "NEW"})
    assert marker.is_new_assembly is True
    assert marker.display == "NEW"
    assert marker.reference == ""


def test_label_and_reference_both_present():
    marker = derive_repeat_marker({"rowc_label": "ROWC", "rowc_ref": "123456"})
    assert marker.label == "ROWC"
    assert marker.reference == "123456"
    assert marker.display == "ROWC 123456"


def test_reference_only():
    marker = derive_repeat_marker({"rowc_ref": "123456"})
    assert marker.label == ""
    assert marker.reference == "123456"
    assert marker.display == "123456"


def test_label_only_leaves_reference_empty():
    """The cue does not fire when the traveler gave no reference."""
    marker = derive_repeat_marker({"rowc_label": "ROWC"})
    assert marker.reference == ""
    assert marker.display == "ROWC"


def test_empty_metadata_synthesises_repeat_without_a_reference():
    marker = derive_repeat_marker({})
    assert marker.display == "REPEAT"
    assert marker.reference == ""
    assert marker.label == ""


def test_multi_word_label_does_not_bleed_into_the_reference():
    """The case a whitespace split on display would get wrong."""
    marker = derive_repeat_marker({"rowc_label": "REPEAT OF", "rowc_ref": "123456"})
    assert marker.label == "REPEAT OF"
    assert marker.reference == "123456"
    assert marker.display == "REPEAT OF 123456"
    assert marker.display.endswith(marker.reference)


def test_blank_reference_is_trimmed_away():
    marker = derive_repeat_marker({"rowc_label": "ROWC", "rowc_ref": "   "})
    assert marker.reference == ""


@pytest.mark.parametrize(
    "meta",
    [
        {},
        {"assembly_type": "NEW"},
        {"assembly_type": "NEW", "rowc_label": "ROWC", "rowc_ref": "9"},
        {"rowc_label": "ROWC"},
        {"rowc_ref": "123456"},
        {"rowc_label": "ROWC", "rowc_ref": "123456"},
        {"rowc_label": "REPEAT OF", "rowc_ref": "123456"},
        {"rowc_label": "  ROWC  ", "rowc_ref": "  123456  "},
    ],
)
def test_display_equals_derive_repeat(meta):
    assert derive_repeat_marker(meta).display == derive_repeat(meta)


def test_str_is_the_display_string():
    assert str(derive_repeat_marker({"rowc_ref": "12"})) == "12"


def test_marker_is_frozen():
    marker = derive_repeat_marker({})
    with pytest.raises(Exception):
        marker.reference = "x"  # type: ignore[misc]
    assert isinstance(marker, RepeatMarker)
