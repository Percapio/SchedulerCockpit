"""Phase 49 section 8.3 — Audit View identity bar substring colouring."""

import pytest

from cockpit.services.repeat import derive_repeat_marker
from cockpit.services.views import AuditIdentityBanner
from cockpit.ui import facelift
from cockpit.ui.widgets.audit_identity_bar import AuditIdentityBar


def banner(assembly_class="Class 3", repeat_meta=None):
    return AuditIdentityBanner(
        sales_order="SO-49",
        part_number="PN-49",
        quantity="10",
        lead_time_days="5",
        assembly_class=assembly_class,
        process="LEAD FREE NO CLEAN",
        customer="TestCorp",
        repeat_marker=derive_repeat_marker(repeat_meta or {}),
        status="NOT_CLEAR",
        is_itar=False,
    )


def test_only_the_digit_of_class_three_is_coloured(qtbot):
    bar = AuditIdentityBar()
    qtbot.addWidget(bar)
    bar.set_identity(banner(assembly_class="Class 3"))

    text = bar.class_lbl.text()
    assert facelift.palette().overdue in text
    assert text.endswith('<span style="color:%s;">3</span>' % facelift.palette().overdue)
    assert "Class " in text.split("<span")[0]


def test_class_two_is_not_coloured(qtbot):
    bar = AuditIdentityBar()
    qtbot.addWidget(bar)
    bar.set_identity(banner(assembly_class="Class 2"))
    assert "<span" not in bar.class_lbl.text()


def test_only_the_reference_is_coloured(qtbot):
    bar = AuditIdentityBar()
    qtbot.addWidget(bar)
    bar.set_identity(banner(repeat_meta={"rowc_label": "ROWC", "rowc_ref": "123456"}))

    text = bar.rowc_lbl.text()
    attention = facelift.palette().attention
    assert f'<span style="color:{attention};">123456</span>' in text
    assert text.split("<span")[0].strip().endswith("ROWC")


def test_reference_without_a_label_still_renders(qtbot):
    bar = AuditIdentityBar()
    qtbot.addWidget(bar)
    bar.set_identity(banner(repeat_meta={"rowc_ref": "123456"}))
    assert "123456" in bar.rowc_lbl.text()


def test_no_repeat_parts_leaves_the_label_empty(qtbot):
    bar = AuditIdentityBar()
    qtbot.addWidget(bar)
    bar.set_identity(banner(repeat_meta={}))
    assert bar.rowc_lbl.text() == ""


def test_markup_in_traveler_text_is_escaped_not_parsed(qtbot):
    """The reference comes off a share other people write to."""
    bar = AuditIdentityBar()
    qtbot.addWidget(bar)
    bar.set_identity(
        banner(repeat_meta={"rowc_label": "A&B", "rowc_ref": "12<3>456"})
    )

    markup = bar.rowc_lbl.text()
    assert "&lt;3&gt;" in markup
    assert "&amp;" in markup
    assert "<3>" not in markup
    # Qt strips the tags it parses; every character must survive to the reader.
    assert "12<3>456" in bar.rowc_lbl.text().replace("&lt;", "<").replace("&gt;", ">")


def test_markup_in_class_text_is_escaped(qtbot):
    bar = AuditIdentityBar()
    qtbot.addWidget(bar)
    bar.set_identity(banner(assembly_class="<b>Class 3"))
    assert "&lt;b&gt;" in bar.class_lbl.text()


@pytest.mark.parametrize("preset", [facelift.DARK, facelift.LIGHT])
def test_cues_track_the_active_preset(qtbot, preset):
    previous = facelift.active_preset()
    facelift.set_active_preset(preset)
    try:
        bar = AuditIdentityBar()
        qtbot.addWidget(bar)
        bar.set_identity(
            banner(repeat_meta={"rowc_label": "ROWC", "rowc_ref": "123456"})
        )
        assert facelift.palette(preset).attention in bar.rowc_lbl.text()
        assert facelift.palette(preset).overdue in bar.class_lbl.text()
    finally:
        facelift.set_active_preset(previous)
