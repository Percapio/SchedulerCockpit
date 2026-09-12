import re
from typing import Union
from .types import MpnKey, Unkeyable

MPN_KEY_GRAMMAR = re.compile(r"^[\x21-\x7E](?:[\x20-\x7E]*[\x21-\x7E])?$")
NORMALISATION_VERSION: int = 1

def normalise_mpn(raw_mpn: str) -> Union[MpnKey, Unkeyable]:
    if not raw_mpn:
        return Unkeyable(raw_mpn)
        
    for char in raw_mpn:
        if ord(char) < 0x20 or ord(char) > 0x7E:
            return Unkeyable(raw_mpn)
            
    # strip leading and trailing whitespace; collapse each internal whitespace run to a single space; uppercase
    stripped = raw_mpn.strip()
    if not stripped:
        return Unkeyable(raw_mpn)
        
    collapsed = re.sub(r"\s+", " ", stripped)
    uppercased = collapsed.upper()
    
    if not MPN_KEY_GRAMMAR.match(uppercased):
        return Unkeyable(raw_mpn)
        
    return uppercased
