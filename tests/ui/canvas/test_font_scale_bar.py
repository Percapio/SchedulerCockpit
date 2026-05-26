import pytest
from cockpit.ui.canvas.font_scale_bar import FontScaleBar

def test_font_scale_bar_signals(qtbot):
    bar = FontScaleBar()
    qtbot.addWidget(bar)
    
    with qtbot.waitSignal(bar.scale_decrease_requested):
        bar._btn_dec.click()
        
    with qtbot.waitSignal(bar.scale_increase_requested):
        bar._btn_inc.click()

def test_font_scale_bar_update_display(qtbot):
    bar = FontScaleBar()
    qtbot.addWidget(bar)
    
    bar.update_display(120)
    assert bar._lbl_percent.text() == "120%"
