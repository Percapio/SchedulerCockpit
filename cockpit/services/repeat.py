from typing import Any, Dict

def derive_repeat(meta: Dict[str, Any]) -> str:
    """
    Derives the repeat string from traveler metadata.
    "NEW" when assembly_type == "NEW", else join([rowc_label, rowc_ref, assembly_modifier]).
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
        
    modifier = str(meta.get("assembly_modifier") or "").strip()
    if modifier:
        parts.append(modifier)
        
    return " ".join(parts) if parts else "REPEAT"
