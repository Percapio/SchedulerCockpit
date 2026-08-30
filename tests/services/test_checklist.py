import pytest
from unittest.mock import Mock

from cockpit.services.checklist import ChecklistService, RefDesIndexCache, BomSourceFileMemo
from cockpit.persistence.types import SourceFileCategory

def test_cache_invalidation_on_load_active_audit():
    mock_audit_repo = Mock()
    mock_source_file_repo = Mock()
    mock_tht_repo = Mock()
    mock_notes_repo = Mock()
    mock_bom_component_repo = Mock()
    mock_conn = Mock()
    
    mock_audit = Mock()
    mock_audit.id = 1
    mock_audit_repo.find_by_id.return_value = mock_audit

    service = ChecklistService(
        conn=mock_conn,
        audit_repo=mock_audit_repo,
        tht_repo=mock_tht_repo,
        notes_repo=mock_notes_repo,
        source_file_repo=mock_source_file_repo,
        bom_component_repo=mock_bom_component_repo,
        image_cache_service=Mock(),
        app_config=Mock()
    )
    
    # Pre-populate caches to simulate previous state
    service._refdes_index_cache._by_source_file[999] = {"M1": (1, ("R1",))}
    service._bom_source_file_memo._by_audit[1] = 999
    
    mock_bom_sf = Mock()
    mock_bom_sf.id = 1000
    mock_bom_sf.file_category = SourceFileCategory.BOM.value
    mock_source_file_repo.list_for_audit.return_value = [mock_bom_sf]
    
    mock_tht_repo.list_for_audit.return_value = []
    mock_notes_repo.list_for_audit.return_value = []
    mock_bom_component_repo.list_for_source_file.return_value = []
    
    service.load_active_audit(1)
    
    # Assert caches were cleared before being repopulated
    assert 999 not in service._refdes_index_cache._by_source_file
    assert service._bom_source_file_memo._by_audit[1] == 1000
