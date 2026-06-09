from typing import Any, Dict

def derive_repeat(meta: Dict[str, Any]) -> str:
    """
    Derives the repeat string from traveler metadata.
    "NEW" when assembly_type == "NEW", else join([rowc_label, rowc_ref]).
    """
    if meta.get("assembly_type") == "NEW":
        return "NEW"
        
    parts = []
    
    rowc_label = str(meta.get("rowc_label") or "").strip()
    if rowc_label:
        parts.append(rowc_label)
        
    rowc_ref = str(meta.get("rowc_ref") or "").strip()
    if rowc_ref:
        parts.append(rowc_ref)
        
    return " ".join(parts) if parts else "REPEAT"
