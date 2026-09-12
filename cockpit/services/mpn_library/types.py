from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ValueKind(Enum):
    NUMERIC = "NUMERIC"
    TEXT = "TEXT"
    ENUM = "ENUM"


class AttributeGroup(Enum):
    CARRIER = "CARRIER"
    PACKAGE = "PACKAGE"
    BODY = "BODY"
    HANDLING = "HANDLING"


class Provenance(Enum):
    OPERATOR_OVERRIDE = "OPERATOR_OVERRIDE"
    REFERENCE_SHEET = "REFERENCE_SHEET"
    DIGIKEY = "DIGIKEY"


MpnKey = str
AttributeName = str


@dataclass(frozen=True)
class AttributeSpec:
    name: AttributeName
    display: str
    unit: Optional[str]
    value_kind: ValueKind
    group: AttributeGroup


@dataclass(frozen=True)
class PartAttributeDraft:
    mpn_key: MpnKey
    attribute_name: AttributeName
    provenance: Provenance
    value_text: str
    value_numeric: Optional[float]
    source_label: Optional[str]


@dataclass(frozen=True)
class ResolvedAttribute:
    mpn_key: MpnKey
    attribute_name: AttributeName
    provenance: Provenance
    value_text: str
    value_numeric: Optional[float]
    source_label: Optional[str]
    recorded_at: str
    confirmed_at: Optional[str]


class Absent:
    """Represents an attribute with no supplied value from any provenance."""
    pass


@dataclass(frozen=True)
class ObservationSummary:
    new_unresolved_count: int
    new_unkeyable_count: int
    existing_count: int


class Unkeyable:
    """Returned by normalise_mpn when the MPN cannot be keyed."""
    def __init__(self, raw_mpn: str):
        self.raw_mpn = raw_mpn
