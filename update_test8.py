import re

def update():
    with open('tests/services/test_phase45_ingestion.py', 'r', encoding='utf-8') as f:
        content = f.read()

    content = content.replace(
        "INSERT INTO source_files (id, audit_id, original_name, local_storage_path, file_category, hash_sha256) VALUES (1, 1, 'f', 'f', 'BOM', 'h')",
        "INSERT INTO source_files (id, audit_id, original_filename, local_storage_path, file_category, file_hash, ingested_at) VALUES (1, 1, 'f', 'f', 'BOM', 'h', '2025-01-01T00:00:00Z')"
    )

    with open('tests/services/test_phase45_ingestion.py', 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == '__main__':
    update()
