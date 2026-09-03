import re

def update():
    with open('cockpit/services/checklist.py', 'r', encoding='utf-8') as f:
        content = f.read()
    
    if 'from cockpit.utils.sorting import natural_sort_key' not in content:
        content = 'from cockpit.utils.sorting import natural_sort_key\n' + content

    content = content.replace(
        'tuple(sorted(data["ref_des_list"]))',
        'tuple(sorted(data["ref_des_list"], key=natural_sort_key))'
    )

    content = content.replace(
        'tuple[bool, int, int]',
        'tuple' # We can just drop the specific return type annotation or use tuple
    )

    content = content.replace(
        'return (row_view.find_number is None, row_view.find_number or 0, row_view.key.item_id)',
        'return (row_view.find_number is None, natural_sort_key(row_view.find_number), row_view.key.item_id)'
    )

    old_tht_count = 'tht_placement_count: int = sum(len(ref_des_list) for _, ref_des_list in tht_index.values())'
    new_tht_count = '''tht_placement_count: int = len({
            ref_des
            for _, ref_des_list in tht_index.values()
            for ref_des in ref_des_list
        })'''
    content = content.replace(old_tht_count, new_tht_count)

    with open('cockpit/services/checklist.py', 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == '__main__':
    update()
