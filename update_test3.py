import re

def update():
    with open('tests/services/test_phase45_ingestion.py', 'r', encoding='utf-8') as f:
        content = f.read()

    patch = '''    tht_repo = ThtChecklistRepository(conn)
    class DummyLayoutParser:
        def parse(self, path, ref): return []
    svc = IngestionService(
        conn=conn,
        audit_repo=audit_repo,
        source_file_repo=sf_repo,
        tht_repo=tht_repo,
        bom_component_repo=bom_repo,
        pdf_coord_repo=pdf_repo,
        layout_parser=DummyLayoutParser(),
        coord_map=None,
        file_storage_root=tmp_path / "storage"
    )
    
    # monkeypatch audit_bom.parse
    import cockpit.ingestion.parsers.audit_bom
    def mock_parse(path):
        return parser.parse(path)
    
    monkeypatch.setattr(cockpit.ingestion.parsers.audit_bom, "parse", mock_parse)'''

    content = re.sub(r'    tht_repo = ThtChecklistRepository\(conn\).*?parser_registry=registry\n    \)', patch, content, flags=re.DOTALL)

    with open('tests/services/test_phase45_ingestion.py', 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == '__main__':
    update()
