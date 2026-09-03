import re

def update():
    with open('tests/services/test_phase45_ingestion.py', 'r', encoding='utf-8') as f:
        content = f.read()

    content = content.replace('ref_des_list=["R14"]', 'ref_des_list=["R14"], ref_des_raw="R14"')
    content = content.replace('ref_des_list=["R14", "R14"]', 'ref_des_list=["R14", "R14"], ref_des_raw="R14, R14"')

    with open('tests/services/test_phase45_ingestion.py', 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == '__main__':
    update()
