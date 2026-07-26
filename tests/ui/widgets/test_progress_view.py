import pytest
from PyQt6.QtWidgets import QWidget
from cockpit.ingestion.progress import ProgressStage
from cockpit.ui.widgets.progress_view import ProgressView

def test_progress_view_advance_with_annotation(qapp):
    stages = [ProgressStage.GATEKEEPER_PASSED, ProgressStage.BOM_PARSED]
    view = ProgressView(stages)
    
    icon_label, text_label = view.stage_labels[ProgressStage.BOM_PARSED]
    assert text_label.text() == "Bom Parsed"
    
    view.advance(ProgressStage.BOM_PARSED, "legacy layout")
    assert text_label.text() == "Bom Parsed — legacy layout"
    assert icon_label.text() == "✓"
    assert text_label.property("status") == "completed"

def test_progress_view_advance_without_annotation(qapp):
    stages = [ProgressStage.GATEKEEPER_PASSED, ProgressStage.BOM_PARSED]
    view = ProgressView(stages)
    
    icon_label, text_label = view.stage_labels[ProgressStage.GATEKEEPER_PASSED]
    assert text_label.text() == "Gatekeeper Passed"
    
    view.advance(ProgressStage.GATEKEEPER_PASSED)
    assert text_label.text() == "Gatekeeper Passed"
    assert icon_label.text() == "✓"

def test_progress_view_reset_clears_annotation(qapp):
    stages = [ProgressStage.BOM_PARSED]
    view = ProgressView(stages)
    
    icon_label, text_label = view.stage_labels[ProgressStage.BOM_PARSED]
    view.advance(ProgressStage.BOM_PARSED, "legacy layout")
    assert text_label.text() == "Bom Parsed — legacy layout"
    
    view.reset()
    assert text_label.text() == "Bom Parsed"
    assert icon_label.text() == "○"
    assert text_label.property("status") == "pending"
