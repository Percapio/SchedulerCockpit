import re

def update():
    with open('cockpit/services/second_ops.py', 'r', encoding='utf-8') as f:
        content = f.read()

    if 'from cockpit.utils.sorting import natural_sort_key' not in content:
        content = 'from cockpit.utils.sorting import natural_sort_key\n' + content

    old_result = '''    result = []
    for audit_id, data in grouped.items():
        if data["candidates"]:
            result.append(AuditCandidates(
                audit_id=audit_id,
                part_number=data["part_number"],
                work_order_ref=data["work_order_ref"],
                candidates=data["candidates"]
            ))'''

    new_result = '''    result = []
    for audit_id, data in grouped.items():
        if data["candidates"]:
            data["candidates"].sort(key=lambda c: natural_sort_key(c.find_number))
            result.append(AuditCandidates(
                audit_id=audit_id,
                part_number=data["part_number"],
                work_order_ref=data["work_order_ref"],
                candidates=data["candidates"]
            ))'''

    content = content.replace(old_result, new_result)

    with open('cockpit/services/second_ops.py', 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == '__main__':
    update()
