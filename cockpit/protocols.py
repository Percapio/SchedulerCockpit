"""Cross-module protocols for dependency injection."""

import pathlib
import typing
from dataclasses import dataclass


@typing.runtime_checkable
class BomParserProtocol(typing.Protocol):
    """Structural duck-type for the BOM parser."""
    def parse(self, path: pathlib.Path) -> "BomResult": ...


@dataclass(frozen=True)
class ParserRegistry:
    """Registry of parser instances."""
    bom_parser:        BomParserProtocol
    eco_parser:        typing.Any
    traveler_parser:   typing.Any
    pdf_layout_parser: typing.Any
    coord_map:         typing.Any
