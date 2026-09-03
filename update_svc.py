import re

def update():
    with open('tests/services/test_phase45_ingestion.py', 'r', encoding='utf-8') as f:
        content = f.read()

    content = content.replace(
        'svc = IngestionService(conn, registry, audit_repo, sf_repo, bom_repo, pdf_repo, tmp_path / "storage")',
        'svc = IngestionService(conn, registry, audit_repo, sf_repo, bom_repo, pdf_repo, None, tmp_path / "storage")'
    )

    with open('tests/services/test_phase45_ingestion.py', 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == '__main__':
    update()
