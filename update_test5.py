import re

def update():
    with open('tests/services/test_phase45_ingestion.py', 'r', encoding='utf-8') as f:
        content = f.read()

    content = content.replace(
        "(id, part_number, work_order_ref, quantity) VALUES (1, 'PN', 'WO', 1)",
        "(id, part_number, work_order_ref, quantity, created_at) VALUES (1, 'PN', 'WO', 1, '2025-01-01T00:00:00Z')"
    )

    with open('tests/services/test_phase45_ingestion.py', 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == '__main__':
    update()
