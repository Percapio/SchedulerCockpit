import pytest
from pathlib import Path
from cockpit.services.notes_renderer import render_build_notes, RenderPalette

def test_document_missing(tmp_path):
    palette = RenderPalette('#FFF', '#000', '#F00', '#F00')
    res = render_build_notes(tmp_path / 'missing.docx', palette)
    assert not res.is_ok()
    assert res.err.reason == 'DocumentMissing'

def test_document_unreadable(tmp_path):
    f = tmp_path / 'bad.docx'
    f.write_text('not a docx')
    palette = RenderPalette('#FFF', '#000', '#F00', '#F00')
    res = render_build_notes(f, palette)
    assert not res.is_ok()
    assert res.err.reason == 'DocumentUnreadable'

def test_valid_document(tmp_path):
    import docx
    d = docx.Document()
    t = d.add_table(2, 2)
    t.cell(0, 0).text = 'Hello'
    t.cell(0, 1).text = 'World'
    
    f = tmp_path / 'valid.docx'
    d.save(str(f))
    
    palette = RenderPalette('#FFF', '#000', '#F00', '#F00')
    res = render_build_notes(f, palette)
    assert res.is_ok()
    assert 'Hello' in res.ok.document.toPlainText()
