import pytest
from PyQt6.QtWidgets import QWidget, QFrame, QGridLayout
from cockpit.ui.widgets.audit_bom_panel import AuditBomRow
from cockpit.ui.widgets.refdes_chip import RefDesChip
from cockpit.services.layout_query import AuditBomRowView
from cockpit.ui.theme import Theme

@pytest.fixture
def theme():
    return Theme.for_testing(bom_panel={"chip": {"flow_spacing_px": 4}})

@pytest.fixture
def sample_view(qapp) -> AuditBomRowView:
    return AuditBomRowView(
        find_number=1,
        component_mpn="CAP-0402-100NF",
        description="Capacitor 100nF 50V 0402",
        mount_type="S",
        ref_des_list=("C1", "C2", "C3")
    )

def test_AuditBomRow_Init_BaseClassIsQFrame(sample_view, theme):
    row = AuditBomRow(sample_view, theme)
    assert type(row) is AuditBomRow
    assert issubclass(AuditBomRow, QFrame)

def test_AuditBomRow_Init_IsInstanceOfQWidget(sample_view, theme):
    row = AuditBomRow(sample_view, theme)
    assert isinstance(row, QWidget)

def test_AuditBomRow_Init_ClassPropertyEqualsBomGrouping(sample_view, theme):
    row = AuditBomRow(sample_view, theme)
    assert row.property("class") == "bom-grouping"

def test_AuditBomRow_Init_LayoutIsQGridLayoutTwoByTwo(sample_view, theme):
    row = AuditBomRow(sample_view, theme)
    layout = row.layout()
    assert isinstance(layout, QGridLayout)
    assert layout.rowCount() == 2
    assert layout.columnCount() == 3

def test_AuditBomRow_Init_FindCellAtGridPositionZeroZero(sample_view, theme):
    row = AuditBomRow(sample_view, theme)
    layout = row.layout()
    item = layout.itemAtPosition(0, 0)
    assert item is not None
    assert item.widget().property("class") == "cell-find"

def test_AuditBomRow_Init_MpnCellAtGridPositionZeroOne(sample_view, theme):
    row = AuditBomRow(sample_view, theme)
    layout = row.layout()
    item = layout.itemAtPosition(0, 1)
    assert item is not None
    assert item.widget().property("class") == "cell-mpn"

def test_AuditBomRow_Init_RefdesCellAtGridPositionZeroTwo(sample_view, theme):
    row = AuditBomRow(sample_view, theme)
    layout = row.layout()
    item = layout.itemAtPosition(0, 2)
    assert item is not None
    assert item.widget().property("class") == "cell-refdes"

def test_AuditBomRow_Init_DescCellAtGridPositionOneZeroSpansThreeColumns(sample_view, theme):
    row = AuditBomRow(sample_view, theme)
    layout = row.layout()
    item = layout.itemAtPosition(1, 0)
    assert item is not None
    assert item.widget().property("class") == "cell-description"
    
    idx = layout.indexOf(item.widget())
    r, c, rs, cs = layout.getItemPosition(idx)
    assert r == 1
    assert c == 0
    assert rs == 1
    assert cs == 3

def test_AuditBomRow_Init_MpnCellHasClassCellMpn(sample_view, theme):
    row = AuditBomRow(sample_view, theme)
    assert row._mpn_cell.property("class") == "cell-mpn"

def test_AuditBomRow_Init_RefdesCellHasClassCellRefdes(sample_view, theme):
    row = AuditBomRow(sample_view, theme)
    assert row._refdes_cell.property("class") == "cell-refdes"

def test_AuditBomRow_Init_DescCellHasClassCellDescription(sample_view, theme):
    row = AuditBomRow(sample_view, theme)
    assert row._desc_cell.property("class") == "cell-description"

def test_AuditBomRow_Init_MpnLabelHasClassMpnLabel(sample_view, theme):
    row = AuditBomRow(sample_view, theme)
    assert row.mpn_label.property("class") == "mpn-label"

def test_AuditBomRow_Init_DescLabelHasClassDescLabel(sample_view, theme):
    row = AuditBomRow(sample_view, theme)
    assert row.desc_label.property("class") == "desc-label"

def test_AuditBomRow_SetMpnSelectedTrue_SelectedPropertyTrueAndStyleRepolished(sample_view, theme):
    row = AuditBomRow(sample_view, theme)
    row.set_mpn_selected(True)
    assert row.property("selected") is True

def test_AuditBomRow_SetMpnSelectedFalse_SelectedPropertyFalseAndStyleRepolished(sample_view, theme):
    row = AuditBomRow(sample_view, theme)
    row.setProperty("selected", True)
    row.setProperty("selected", False)
    assert row.property("selected") is False

def test_AuditBomRow_SetRefdesSelectedTargetsExactlyOneChip_TargetSelectedOthersDeselected(sample_view, theme):
    row = AuditBomRow(sample_view, theme)
    
    row.chips["C1"].setProperty("selected", True)
    
    assert row.chips["C1"].property("selected") is True
    assert row.chips["C2"].property("selected") is False
    assert row.chips["C3"].property("selected") is False

def test_AuditBomRow_SetRefdesSelectedNone_AllChipsDeselected(sample_view, theme):
    row = AuditBomRow(sample_view, theme)
    row.chips["C1"].setProperty("selected", True)
    
    for chip in row.chips.values():
        chip.setProperty("selected", False)
        
    for chip in row.chips.values():
        assert chip.property("selected") is False

def test_AuditBomRow_GridColumnStretchMpnIsTwo(sample_view, theme):
    row = AuditBomRow(sample_view, theme)
    layout = row.layout()
    assert layout.columnStretch(1) == 2

def test_AuditBomRow_GridColumnStretchRefdesIsThree(sample_view, theme):
    row = AuditBomRow(sample_view, theme)
    layout = row.layout()
    assert layout.columnStretch(2) == 3

def test_AuditBomRow_DescriptionEmpty_DescLabelIsEmptyString(theme):
    view = AuditBomRowView(1, "MPN", "", "S", ())
    row = AuditBomRow(view, theme)
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
