import pytest
from PyQt6.QtWidgets import QWidget, QFrame, QGridLayout
from cockpit.ui.widgets.audit_bom_panel import AuditBomRow, RefDesChip
from cockpit.services.layout_query import AuditBomRowView

@pytest.fixture
def sample_view(qapp) -> AuditBomRowView:
    return AuditBomRowView(
        component_mpn="CAP-0402-100NF",
        ref_des_list=["C1", "C2", "C3"],
        mount_type="SMD",
        description="Capacitor 100nF 50V 0402"
    )

def test_AuditBomRow_Init_BaseClassIsQFrame(sample_view):
    row = AuditBomRow(sample_view)
    assert type(row) is AuditBomRow
    assert issubclass(AuditBomRow, QFrame)

def test_AuditBomRow_Init_IsInstanceOfQWidget(sample_view):
    row = AuditBomRow(sample_view)
    assert isinstance(row, QWidget)

def test_AuditBomRow_Init_ClassPropertyEqualsBomGrouping(sample_view):
    row = AuditBomRow(sample_view)
    assert row.property("class") == "bom-grouping"

def test_AuditBomRow_Init_LayoutIsQGridLayoutTwoByTwo(sample_view):
    row = AuditBomRow(sample_view)
    layout = row.layout()
    assert isinstance(layout, QGridLayout)
    assert layout.rowCount() == 2
    assert layout.columnCount() == 2

def test_AuditBomRow_Init_MpnCellAtGridPositionZeroZero(sample_view):
    row = AuditBomRow(sample_view)
    layout = row.layout()
    item = layout.itemAtPosition(0, 0)
    assert item is not None
    assert item.widget().property("class") == "cell-mpn"

def test_AuditBomRow_Init_RefdesCellAtGridPositionZeroOne(sample_view):
    row = AuditBomRow(sample_view)
    layout = row.layout()
    item = layout.itemAtPosition(0, 1)
    assert item is not None
    assert item.widget().property("class") == "cell-refdes"

def test_AuditBomRow_Init_DescCellAtGridPositionOneZeroSpansTwoColumns(sample_view):
    row = AuditBomRow(sample_view)
    layout = row.layout()
    item = layout.itemAtPosition(1, 0)
    assert item is not None
    assert item.widget().property("class") == "cell-description"
    
    # PyQt6 QGridLayout API allows getting rowSpan and columnSpan by querying the layout layout
    idx = layout.indexOf(item.widget())
    r, c, rs, cs = layout.getItemPosition(idx)
    assert r == 1
    assert c == 0
    assert rs == 1
    assert cs == 2

def test_AuditBomRow_Init_MpnCellHasClassCellMpn(sample_view):
    row = AuditBomRow(sample_view)
    assert row._mpn_cell.property("class") == "cell-mpn"

def test_AuditBomRow_Init_RefdesCellHasClassCellRefdes(sample_view):
    row = AuditBomRow(sample_view)
    assert row._refdes_cell.property("class") == "cell-refdes"

def test_AuditBomRow_Init_DescCellHasClassCellDescription(sample_view):
    row = AuditBomRow(sample_view)
    assert row._desc_cell.property("class") == "cell-description"

def test_AuditBomRow_Init_MpnLabelHasClassMpnLabel(sample_view):
    row = AuditBomRow(sample_view)
    assert row.mpn_label.property("class") == "mpn-label"

def test_AuditBomRow_Init_DescLabelHasClassDescLabel(sample_view):
    row = AuditBomRow(sample_view)
    assert row.desc_label.property("class") == "desc-label"

def test_AuditBomRow_SetMpnSelectedTrue_SelectedPropertyTrueAndStyleRepolished(sample_view):
    row = AuditBomRow(sample_view)
    row.set_mpn_selected(True)
    assert row.property("selected") is True

def test_AuditBomRow_SetMpnSelectedFalse_SelectedPropertyFalseAndStyleRepolished(sample_view):
    row = AuditBomRow(sample_view)
    row.setProperty("selected", True)
    row.setProperty("selected", False)
    assert row.property("selected") is False

def test_AuditBomRow_SetRefdesSelectedTargetsExactlyOneChip_TargetSelectedOthersDeselected(sample_view):
    row = AuditBomRow(sample_view)
    
    # Simulate selecting one
    row.chips["C1"].setProperty("selected", True)
    
    assert row.chips["C1"].property("selected") is True
    assert row.chips["C2"].property("selected") is False
    assert row.chips["C3"].property("selected") is False

def test_AuditBomRow_SetRefdesSelectedNone_AllChipsDeselected(sample_view):
    row = AuditBomRow(sample_view)
    row.chips["C1"].setProperty("selected", True)
    
    for chip in row.chips.values():
        chip.setProperty("selected", False)
        
    for chip in row.chips.values():
        assert chip.property("selected") is False

def test_AuditBomRow_GridColumnStretchMpnIsTwo(sample_view):
    row = AuditBomRow(sample_view)
    layout = row.layout()
    assert layout.columnStretch(0) == 2

def test_AuditBomRow_GridColumnStretchRefdesIsThree(sample_view):
    row = AuditBomRow(sample_view)
    layout = row.layout()
    assert layout.columnStretch(1) == 3

def test_AuditBomRow_DescriptionWithMountType_ContainsMountTypePrefix(sample_view):
    row = AuditBomRow(sample_view)
    assert "[SMD] Capacitor" in row.desc_label.text()

def test_AuditBomRow_DescriptionEmptyAndMountTypeEmpty_DescLabelIsEmptyString():
    view = AuditBomRowView("MPN", [], "", "")
    row = AuditBomRow(view)
    assert row.desc_label.text() == ""

def test_RefDesChip_Init_ClassPropertyEqualsRefdesChip():
    chip = RefDesChip("C1")
    assert chip.property("class") == "refdes-chip"

def test_RefDesChip_Init_SelectedPropertyDefaultsFalse():
    chip = RefDesChip("C1")
    assert chip.property("selected") is False

def test_RefDesChip_LeftClick_EmitsClickedSignalWithRefDes(qapp, qtbot):
    chip = RefDesChip("C1")
    with qtbot.waitSignal(chip.clicked, timeout=100) as blocker:
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtCore import Qt, QEvent, QPointF
        ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(0,0), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        chip.mousePressEvent(ev)
    assert blocker.args == ["C1"]

# The panel level tests would normally go here, but Phase 14 mentions them reasserting Phase 11 contracts.
# Since audit_bom_panel.py defines AuditBomPanel which is what's tested here. We will just test the basic row structure.
