from dataclasses import dataclass
from enum import Enum
from typing import Optional, List
from .types import MpnKey, PartAttributeDraft

class PackagingType(Enum):
    TAPE_AND_REEL = "TAPE_AND_REEL"
    CUT_TAPE = "CUT_TAPE"
    DIGI_REEL = "DIGI_REEL"
    TRAY = "TRAY"
    TUBE = "TUBE"
    BULK = "BULK"
    UNKNOWN = "UNKNOWN"

@dataclass
class VariantCandidate:
    digikey_part_number: str
    manufacturer: Optional[str]
    packaging_label: Optional[str]
    packaging_type: PackagingType
    attributes: List[PartAttributeDraft]

@dataclass
class DigiKeyCredentials:
    client_id: str
    client_secret: str

@dataclass
class RequestBudget:
    remaining: int
    spent: int = 0

    def take(self) -> bool:
        if self.remaining > 0:
            self.remaining -= 1
            self.spent += 1
            return True
        return False

@dataclass
class BearerToken:
    access_token: str
    expires_at: float

@dataclass
class DigiKeyProductQuery:
    keywords: MpnKey

class SnapshotUnusable:
    pass

@dataclass
class Resolved:
    candidate: VariantCandidate
    raw_response: bytes
    http_status: int

@dataclass
class Ambiguous:
    candidates: List[VariantCandidate]
    raw_response: bytes
    http_status: int

@dataclass
class NotFound:
    raw_response: bytes
    http_status: int
