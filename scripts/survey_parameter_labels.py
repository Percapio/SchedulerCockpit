"""Phase 48 section 8.2 -- which DigiKey parameter labels supply a registry name.

A developer step, not a test and not a shipped code path. Run it once against a
local parts_library.db and read the output by hand before editing PARAM_MAPPING.
It needs a populated library on a specific machine, so it cannot be a build
gate and is not wired into CI. It costs no quota: digikey_snapshot retains
every raw response in full for exactly this purpose.

    python scripts/survey_parameter_labels.py [path/to/parts_library.db]

Acceptance, per the phase document:

    fewer than 20 snapshots        survey inconclusive, defer to Phase 49
    20+, label in a clear majority add the mapping
    20+, label absent or rare      do not map; record the registry name as a
                                   known gap and keep its absence under test
"""

import argparse
import json
import os
import pathlib
import sqlite3
import sys
from collections import Counter
from typing import Dict, Iterable, List, NamedTuple, Set

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from cockpit.services.mpn_library.registry import ATTRIBUTE_REGISTRY  # noqa: E402

SOUGHT_ATTRIBUTE_NAMES: Set[str] = {"pin_count", "body_length_mm", "body_width_mm"}

MINIMUM_SNAPSHOTS_FOR_CONFIDENCE = 20

# Tokens that identify a candidate label for a registry name. Deliberately
# hand-written rather than derived: the survey's job is to surface candidates
# for a human to confirm, not to decide.
CANDIDATE_TOKENS: Dict[str, Set[str]] = {
    "pin_count": {"pin", "pins", "position", "positions", "terminal", "terminations"},
    "body_length_mm": {"length", "size", "dimension", "dimensions", "body"},
    "body_width_mm": {"width", "size", "dimension", "dimensions", "body"},
}


class ObservedLabel(NamedTuple):
    label: str
    snapshot_count: int
    sample_value: str


def _tokenise(label: str) -> Set[str]:
    cleaned = "".join(character.lower() if character.isalnum() else " " for character in label)
    return set(cleaned.split())


def _parameter_labels(snapshot_json: str) -> Dict[str, str]:
    """Every parameter label in one raw response, mapped to a sample value."""
    try:
        response = json.loads(snapshot_json)
    except (json.JSONDecodeError, TypeError):
        return {}

    labels: Dict[str, str] = {}
    for product in response.get("Products", []) or []:
        for parameter in product.get("Parameters", []) or []:
            label = parameter.get("Parameter")
            value = parameter.get("Value")
            if not label or not value or value == "-":
                continue
            labels.setdefault(label, value)
    return labels


def survey_parameter_labels(
    snapshots: Iterable[str],
    sought: Set[str]
) -> Dict[str, List[ObservedLabel]]:
    """Confirms which DigiKey parameter labels supply a registry attribute.

    Returns, per registry name sought, every distinct parameter label observed
    that plausibly supplies it, with the count of snapshots carrying that
    label. An empty list for a name no snapshot supplies is a result, not a
    failure.
    """
    label_counts: Counter = Counter()
    label_samples: Dict[str, str] = {}

    for snapshot_json in snapshots:
        for label, sample_value in _parameter_labels(snapshot_json).items():
            label_counts[label] += 1
            label_samples.setdefault(label, sample_value)

    findings: Dict[str, List[ObservedLabel]] = {}
    for attribute_name in sorted(sought):
        tokens = CANDIDATE_TOKENS.get(attribute_name, set())
        matches = [
            ObservedLabel(label, count, label_samples[label])
            for label, count in label_counts.items()
            if tokens & _tokenise(label)
        ]
        findings[attribute_name] = sorted(matches, key=lambda m: -m.snapshot_count)
    return findings


def default_library_path() -> pathlib.Path:
    override = os.environ.get("COCKPIT_APP_DATA")
    root = pathlib.Path(override) if override else pathlib.Path(
        os.environ.get("APPDATA", pathlib.Path.home())
    ) / "Cockpit"
    return root / "v1" / "parts_library.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library_path", nargs="?", type=pathlib.Path,
                        default=default_library_path())
    args = parser.parse_args()

    if not args.library_path.exists():
        print(f"No library at {args.library_path}")
        return 1

    connection = sqlite3.connect(f"file:{args.library_path}?mode=ro", uri=True)
    try:
        snapshots = [row[0] for row in connection.execute(
            "SELECT payload_json FROM digikey_snapshot"
        )]
    finally:
        connection.close()

    print(f"Library:   {args.library_path}")
    print(f"Snapshots: {len(snapshots)}")
    if len(snapshots) < MINIMUM_SNAPSHOTS_FOR_CONFIDENCE:
        print(
            f"\nFewer than {MINIMUM_SNAPSHOTS_FOR_CONFIDENCE} snapshots. The survey is "
            "inconclusive: a label's absence here says more about which parts happen "
            "to be in this library than about what DigiKey returns. Add no mappings."
        )

    findings = survey_parameter_labels(snapshots, SOUGHT_ATTRIBUTE_NAMES)
    for attribute_name, observed in findings.items():
        spec = ATTRIBUTE_REGISTRY[attribute_name]
        print(f"\n{attribute_name}  ({spec.display})")
        if not observed:
            print("    no candidate label observed")
            continue
        for candidate in observed:
            share = candidate.snapshot_count / len(snapshots) if snapshots else 0.0
            print(
                f"    {candidate.label!r}: {candidate.snapshot_count} snapshots "
                f"({share:.0%})  e.g. {candidate.sample_value!r}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
