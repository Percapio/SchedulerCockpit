import re

def update():
    with open('tests/services/test_phase45_ingestion.py', 'r', encoding='utf-8') as f:
        content = f.read()

    import_tht = 'from cockpit.persistence.repositories.tht_components import ThtComponentRepository\n'
    if 'ThtComponentRepository' not in content:
        content = import_tht + content

    patch = '''    tht_repo = ThtComponentRepository(conn)
    class DummyLayoutParser:
        def parse(self, path): return []
    svc = IngestionService(
        conn=conn,
        audit_repo=audit_repo,
        source_file_repo=sf_repo,
        tht_repo=tht_repo,
        bom_component_repo=bom_repo,
        pdf_coord_repo=pdf_repo,
        layout_parser=DummyLayoutParser(),
        file_storage_root=tmp_path / "storage",
        parser_registry=registry
    )'''

    content = re.sub(r'    svc = IngestionService\(conn.*?"storage"\)', patch, content, flags=re.DOTALL)

    with open('tests/services/test_phase45_ingestion.py', 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == '__main__':
    update()
