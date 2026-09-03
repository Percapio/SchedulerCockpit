import re

def update():
    with open('tests/services/test_phase45_ingestion.py', 'r', encoding='utf-8') as f:
        content = f.read()

    content = content.replace(
        "(id, part_number, work_order_ref, quantity, created_at) VALUES (1, 'PN', 'WO', 1, '2025-01-01T00:00:00Z')",
        "(id, part_number, work_order_ref, quantity, created_at, updated_at, state) VALUES (1, 'PN', 'WO', 1, '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z', 'OPEN')"
    )

    with open('tests/services/test_phase45_ingestion.py', 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == '__main__':
    update()
