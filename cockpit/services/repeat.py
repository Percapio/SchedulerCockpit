from dataclasses import dataclass
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


@dataclass(frozen=True)
class RepeatMarker:
    """Repeat identification split into its parts.

    `reference` carries only what the traveler actually supplied, so a view can
    style the referenced job number without string surgery on `display`, and so
    a synthesised fallback never draws the eye to a row whose traveler failed to
    parse.
    """

    label: str
    reference: str
    is_new_assembly: bool
    display: str

    def __str__(self) -> str:
        return self.display


def derive_repeat_marker(meta: Dict[str, Any]) -> RepeatMarker:
    """Derives the repeat marker from traveler metadata.

    pre:  meta is the parsed traveler mapping, possibly empty
    post: reference is non-empty only when rowc_ref was supplied and non-blank;
          is_new_assembly is true only when assembly_type == "NEW";
          display equals derive_repeat(meta) for every input, including the
          synthesised "NEW" and "REPEAT" cases
    """
    meta = meta or {}
    label = str(meta.get("rowc_label") or "").strip()
    reference = str(meta.get("rowc_ref") or "").strip()
    is_new_assembly = meta.get("assembly_type") == "NEW"

    return RepeatMarker(
        label=label,
        reference=reference,
        is_new_assembly=is_new_assembly,
        display=derive_repeat(meta),
    )
