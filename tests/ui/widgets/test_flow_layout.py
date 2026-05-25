import pytest
from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtWidgets import QWidget, QPushButton

from cockpit.ui.widgets.flow_layout import FlowLayout

def test_FlowLayout_Empty_HeightForWidthReturnsZero(qapp):
    w = QWidget()
    layout = FlowLayout(w, margin=0, h_spacing=0, v_spacing=0)
    assert layout.heightForWidth(100) == 0

def test_FlowLayout_SingleItem_FitsOneLine(qapp):
    w = QWidget()
    layout = FlowLayout(w, margin=0, h_spacing=0, v_spacing=0)
    btn = QPushButton()
    btn.setFixedSize(50, 20)
    layout.addWidget(btn)
    
    # 100px width is enough for 50px button
    assert layout.heightForWidth(100) == 20

def test_FlowLayout_TwoItemsFitOneLine_HeightForWidthReturnsLineHeight(qapp):
    w = QWidget()
    layout = FlowLayout(w, margin=0, h_spacing=10, v_spacing=10)
    btn1 = QPushButton()
    btn1.setFixedSize(50, 20)
    layout.addWidget(btn1)
    
    btn2 = QPushButton()
    btn2.setFixedSize(30, 20)
    layout.addWidget(btn2)
    
    # 50 + 10 + 30 = 90px < 100px width, so it fits on one line
    assert layout.heightForWidth(100) == 20

def test_FlowLayout_TwoItemsOverflowWidth_WrapsToSecondLine(qapp):
    w = QWidget()
    layout = FlowLayout(w, margin=0, h_spacing=10, v_spacing=10)
    btn1 = QPushButton()
    btn1.setFixedSize(50, 20)
    layout.addWidget(btn1)
    
    btn2 = QPushButton()
    btn2.setFixedSize(60, 20)
    layout.addWidget(btn2)
    
    # 50 + 10 + 60 = 120px > 100px width, so it wraps.
    # line 1 height = 20, v_spacing = 10, line 2 height = 20. Total = 50.
    assert layout.heightForWidth(100) == 50

def test_FlowLayout_ManyItems_LineCountMatchesAvailableWidth(qapp):
    w = QWidget()
    layout = FlowLayout(w, margin=0, h_spacing=10, v_spacing=10)
    for _ in range(5):
        btn = QPushButton()
        btn.setFixedSize(40, 20)
        layout.addWidget(btn)
    
    # Each item 40 + 10 = 50.
    # At width 100, we fit 2 items per line (40+10+40=90).
    # Line 1: 2 items
    # Line 2: 2 items
    # Line 3: 1 item
    # Heights: 20 + 10 + 20 + 10 + 20 = 80
    assert layout.heightForWidth(100) == 80

def test_FlowLayout_HSpacingSet_ItemsAreSpacedHorizontally(qapp):
    w = QWidget()
    layout = FlowLayout(w, margin=0, h_spacing=15, v_spacing=0)
    btn1 = QPushButton()
    btn1.setFixedSize(10, 10)
    layout.addWidget(btn1)
    
    btn2 = QPushButton()
    btn2.setFixedSize(10, 10)
    layout.addWidget(btn2)
    
    # Run geometry layout
    layout.setGeometry(QRect(0, 0, 100, 100))
    
    # btn1 is at x=0
    # btn2 should be at x = 10 (btn1 width) + 15 (h_spacing) = 25
    assert layout.itemAt(0).geometry().x() == 0
    assert layout.itemAt(1).geometry().x() == 25

def test_FlowLayout_VSpacingSet_LinesAreSpacedVertically(qapp):
    w = QWidget()
    layout = FlowLayout(w, margin=0, h_spacing=0, v_spacing=25)
    btn1 = QPushButton()
    btn1.setFixedSize(60, 10)
    layout.addWidget(btn1)
    
    btn2 = QPushButton()
    btn2.setFixedSize(60, 10)
    layout.addWidget(btn2)
    
    # width 100 forces wrap for the second 60px button
    layout.setGeometry(QRect(0, 0, 100, 100))
    
    # line 1 is at y=0, height=10
    # line 2 should be at y=0 + 10 + 25 (v_spacing) = 35
    assert layout.itemAt(0).geometry().y() == 0
    assert layout.itemAt(1).geometry().y() == 35

def test_FlowLayout_AddItem_CountIncrements(qapp):
    layout = FlowLayout()
    assert layout.count() == 0
    layout.addWidget(QPushButton())
    assert layout.count() == 1

def test_FlowLayout_TakeAt_RemovesItemAndCountDecrements(qapp):
    layout = FlowLayout()
    btn = QPushButton()
    layout.addWidget(btn)
    assert layout.count() == 1
    item = layout.takeAt(0)
    assert item is not None
    assert item.widget() is btn
    assert layout.count() == 0

def test_FlowLayout_MinimumSize_ReturnsLargestItemBoundingRect(qapp):
    w = QWidget()
    layout = FlowLayout(w, margin=5)
    btn1 = QPushButton()
    btn1.setFixedSize(30, 20)
    layout.addWidget(btn1)
    
    btn2 = QPushButton()
    btn2.setFixedSize(60, 40)
    layout.addWidget(btn2)
    
    # min size should be 60x40 plus margins (5 left, 5 right, 5 top, 5 bottom = +10)
    # wait, minimumSize iterates items and expands to largest minimumSize.
    # btn2 minimum size is 60x40
    assert layout.minimumSize().width() == 70
    assert layout.minimumSize().height() == 50

def test_FlowLayout_ExpandingDirections_ReturnsZero(qapp):
    layout = FlowLayout()
    assert layout.expandingDirections() == Qt.Orientation(0)

def test_FlowLayout_HasHeightForWidth_ReturnsTrue(qapp):
    layout = FlowLayout()
    assert layout.hasHeightForWidth() is True
