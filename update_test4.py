import re

def update():
    with open('tests/services/test_phase45_ingestion.py', 'r', encoding='utf-8') as f:
        content = f.read()

    content = content.replace(
        "(id, part_number, work_order_ref, quantity, state) VALUES (1, 'PN', 'WO', 1, 'OPEN')",
        "(id, part_number, work_order_ref, quantity) VALUES (1, 'PN', 'WO', 1)"
    )

    with open('tests/services/test_phase45_ingestion.py', 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == '__main__':
    update()
