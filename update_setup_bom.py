import re

def update():
    with open('cockpit/services/setup_bom.py', 'r', encoding='utf-8') as f:
        content = f.read()

    if 'from cockpit.utils.sorting import natural_sort_key' not in content:
        content = 'from cockpit.utils.sorting import natural_sort_key\n' + content

    content = content.replace(
        'tuple(sorted(matching_refs))',
        'tuple(sorted(matching_refs, key=natural_sort_key))'
    )

    content = content.replace(
        'rows.sort(key=lambda r: r.item_number)',
        'rows.sort(key=lambda r: natural_sort_key(r.item_number))'
    )

    content = content.replace(
        'f"<tr><td>{row.item_number}</td><td>{html.escape(row.part_number)}</td><td>{desc}</td><td>{refs}</td></tr>"',
        'f"<tr><td>{html.escape(str(row.item_number))}</td><td>{html.escape(row.part_number)}</td><td>{desc}</td><td>{refs}</td></tr>"'
    )

    with open('cockpit/services/setup_bom.py', 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == '__main__':
    update()
