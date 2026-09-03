import re

def update_audit_bom():
    with open('cockpit/ingestion/parsers/audit_bom.py', 'r', encoding='utf-8') as f:
        content = f.read()

    if 'import re' not in content:
        content = 'import re\n' + content

    helpers = '''
FIND_NUMBER_GRAMMAR = re.compile(r"^[0-9]+[A-Za-z]?$")

def is_number(raw) -> bool:
    if isinstance(raw, bool):
        return False
    return isinstance(raw, (int, float))

def text_of(raw) -> str:
    return str(raw)

def leading_digits_of(candidate: str) -> int:
    m = re.match(r"^([0-9]+)", candidate)
    return int(m.group(1))

def coerce_find_number(raw, path, mpn: str) -> str:
    if raw is None or text_of(raw).strip() == "":
        raise MalformedBomError(path, "MISSING_FIND_NUMBER", {"mpn": mpn})

    if is_number(raw):
        whole = int(raw)
        if whole != raw or whole < 1:
            raise MalformedBomError(path, "INVALID_FIND_NUMBER", {"mpn": mpn, "raw": raw})
        return text_of(whole)

    candidate = text_of(raw).strip().upper()
    if not FIND_NUMBER_GRAMMAR.match(candidate):
        raise MalformedBomError(path, "INVALID_FIND_NUMBER", {"mpn": mpn, "raw": raw})
    
    if leading_digits_of(candidate) < 1:
        raise MalformedBomError(path, "INVALID_FIND_NUMBER", {"mpn": mpn, "raw": raw})
        
    return candidate
'''

    start_idx = content.find('def coerce_find_number')
    end_idx = content.find('def _workbook_has_strike_runs', start_idx)
    content = content[:start_idx] + helpers + '\n' + content[end_idx:]

    with open('cockpit/ingestion/parsers/audit_bom.py', 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == '__main__':
    update_audit_bom()
