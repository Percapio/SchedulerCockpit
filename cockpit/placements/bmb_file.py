"""Reading BOM Builder's .bmb: the extract Cockpit stores, and the placements it holds.

Phase 57 §5. No Qt and no database. The share is read only through share_call, so Phase 55's
watchdog times every share read and nothing else.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TypeVar, Union

try:  # Round 11: listed in the local .gitignore, so a checkout from GitHub may lack it.
    from cockpit.placements.bmb_cipher import reveal_bom_record
except ImportError:  # pragma: no cover - exercised by patching in tests
    reveal_bom_record = None

logger = logging.getLogger(__name__)

T = TypeVar("T")
ShareCallGuard = Callable[[Callable[[], T]], T]


def direct_share_call(call: Callable[[], T]) -> T:
    """The default guard: calls straight through, as Phase 55's service functions do."""
    return call()


# ---------- limits and constants ----------

@dataclass(frozen=True)
class BmbReadLimits:
    max_share_bytes: int
    max_kept_bytes: int
    max_records: int
    max_decals: int
    max_decal_vertices: int
    max_attachment_sections: int


READ_LIMITS = BmbReadLimits(
    max_share_bytes=640 * 1024 * 1024,
    max_kept_bytes=6 * 1024 * 1024,
    max_records=26000,
    max_decals=200,
    max_decal_vertices=12500,
    max_attachment_sections=16,
)

SUPPORTED_VERSION = "0.56"
EXTRACT_SUFFIX = ".bmbx"
PARTIAL_SUFFIX = ".partial"
MAX_MARKER_LINE_BYTES = 256
MAX_PACKAGE_KEY_CHARS = 64
DESIGNATOR_FIELD_CHARS = 15
CHUNK_BYTES = 1024 * 1024
UNASSIGNED = "UNASSIGNED"

LAYER_MARKERS = ("*TOPSILK", "*TOPPASTE", "*TOPCOPPER", "*BOTSILK", "*BOTPASTE", "*BOTCOPPER")
REQUIRED_ORDER = ("*BOM", "*DECALS", "*BOARD") + LAYER_MARKERS
EXTRA_MARKERS = ("*ORIMARKS*", "*ATTACHEDFILE*")
TAIL_ORDER = ("*PARTTYPES*", "*PARTTYPELIST*", "*END")
KEPT_MARKERS = frozenset({"*BOM", "*DECALS", "*PARTTYPES*", "*PARTTYPELIST*", "*END"})
KNOWN_MARKERS = frozenset(REQUIRED_ORDER + EXTRA_MARKERS + TAIL_ORDER)
VERSION_PREFIX = "*VERSION="


# ---------- results ----------

@dataclass(frozen=True)
class PlacementTriplet:
    x_mils: int
    y_mils: int
    rotation_ccw_degrees: int


@dataclass(frozen=True)
class BodyRectangle:
    centre_x_mils: float
    centre_y_mils: float
    width_mils: float
    height_mils: float


@dataclass(frozen=True)
class BmbPlacement:
    designator: str
    placement: PlacementTriplet
    side: str            # 'T' or 'B'
    is_stuffed: bool
    mount: str           # 'S' or 'T'
    part_number: str
    package_key: Optional[str]
    body: Optional[BodyRectangle]


@dataclass(frozen=True)
class BmbProject:
    format_version: str
    placements: tuple[BmbPlacement, ...]
    unplaced: frozenset[str]


@dataclass(frozen=True)
class BmbExtract:
    stored_path: pathlib.Path
    file_hash: str
    source_hash: str
    source_size: int
    source_modified_ns: int
    kept_bytes: int
    skipped_bytes: int


# ---------- rejections ----------

@dataclass(frozen=True)
class UnsupportedVersion:
    found: str


@dataclass(frozen=True)
class UnknownMarker:
    line_index: int


@dataclass(frozen=True)
class SectionMissing:
    section: str


@dataclass(frozen=True)
class SectionRepeated:
    section: str


@dataclass(frozen=True)
class SectionOutOfOrder:
    section: str
    follows: str


@dataclass(frozen=True)
class ContentAfterEnd:
    line_index: int


@dataclass(frozen=True)
class DecodeCheckFailed:
    first_mismatch_index: int


@dataclass(frozen=True)
class RecordUnreadable:
    record_index: int


@dataclass(frozen=True)
class DuplicateDesignator:
    record_index: int


@dataclass(frozen=True)
class ExceedsLimit:
    limit: str


@dataclass(frozen=True)
class DecoderUnavailable:
    pass


BmbRejected = Union[
    UnsupportedVersion, UnknownMarker, SectionMissing, SectionRepeated, SectionOutOfOrder,
    ContentAfterEnd, DecodeCheckFailed, RecordUnreadable, DuplicateDesignator, ExceedsLimit,
    DecoderUnavailable,
]

REJECTION_TYPES: dict[str, type] = {
    cls.__name__: cls for cls in (
        UnsupportedVersion, UnknownMarker, SectionMissing, SectionRepeated, SectionOutOfOrder,
        ContentAfterEnd, DecodeCheckFailed, RecordUnreadable, DuplicateDesignator, ExceedsLimit,
        DecoderUnavailable,
    )
}


@dataclass(frozen=True)
class Extracted:
    extract: BmbExtract
    moved_by_this_run: bool


@dataclass(frozen=True)
class Rejected:
    reason: BmbRejected


@dataclass(frozen=True)
class ShareReadFailed:
    cause: str


@dataclass(frozen=True)
class StorageFailed:
    cause: str


@dataclass(frozen=True)
class Stopped:
    pass


ExtractOutcome = Union[Extracted, Rejected, ShareReadFailed, StorageFailed, Stopped]


@dataclass(frozen=True)
class ExtractMissing:
    pass


@dataclass(frozen=True)
class ExtractCorrupt:
    found_hash: str


@dataclass(frozen=True)
class ExtractUnreadable:
    cause: str


StoredExtractUnusable = Union[ExtractMissing, ExtractCorrupt, ExtractUnreadable]


def describe_error(error: BaseException) -> str:
    """A cause fit for logs and summaries: the exception's kind and code, never its message.

    Messages carry share paths, and share paths carry job folder names (Phase 55's _describe).
    """
    code = getattr(error, "winerror", None) or getattr(error, "errno", None)
    if code is not None:
        return f"{type(error).__name__} ({code})"
    return type(error).__name__


# ---------- the framer: a streaming state machine over the share file ----------

class _Rejection(Exception):
    def __init__(self, reason: Any) -> None:
        super().__init__(type(reason).__name__)
        self.reason = reason


class _Framer:
    """Checks the framing contract while copying the kept sections (§5.2, §5.4).

    Skipped sections are never held: a line is looked at only up to MAX_MARKER_LINE_BYTES when it
    starts with '*', and otherwise only its first byte, so memory stays bounded whatever a line's
    length. Kept bytes go to `sink` and `kept_hasher`, bounded by max_kept_bytes.
    """

    def __init__(self, limits: BmbReadLimits, sink: Callable[[bytes], None]) -> None:
        self.limits = limits
        self.sink = sink
        self.kept_hasher = hashlib.sha256()
        self.kept_bytes = 0
        self.line_index = 0
        self.keeping = True
        self.after_end = False
        self.version_seen = False
        self.required_next = 0
        self.tail_next = 0
        self.last_marker = VERSION_PREFIX
        self.attachments = 0
        self.at_line_start = True
        self.mode = "content"           # content | marker | skip_rest
        self.marker_buffer = bytearray()
        self.line_has_text = False

    # -- kept output --
    def _keep(self, data: bytes) -> None:
        if not data:
            return
        self.kept_bytes += len(data)
        if self.kept_bytes > self.limits.max_kept_bytes:
            raise _Rejection(ExceedsLimit("max_kept_bytes"))
        self.kept_hasher.update(data)
        self.sink(data)

    # -- feeding --
    def feed(self, chunk: bytes) -> None:
        position = 0
        size = len(chunk)
        while position < size:
            newline = chunk.find(b"\n", position)
            end = size if newline < 0 else newline
            segment = chunk[position:end]
            if self.at_line_start and segment:
                self._begin_line(segment[:1])
            if segment:
                self._continue_line(segment)
            if newline < 0:
                return
            self._end_line()
            position = newline + 1

    def finish(self) -> None:
        if not self.at_line_start:
            self._end_line(final=True)
        if not self.version_seen:
            raise _Rejection(SectionMissing(VERSION_PREFIX))
        if self.required_next < len(REQUIRED_ORDER):
            raise _Rejection(SectionMissing(REQUIRED_ORDER[self.required_next]))
        if self.tail_next < len(TAIL_ORDER):
            raise _Rejection(SectionMissing(TAIL_ORDER[self.tail_next]))

    def _begin_line(self, first: bytes) -> None:
        self.at_line_start = False
        if self.line_index == 0 and first != b"*":
            raise _Rejection(SectionMissing(VERSION_PREFIX))
        if first == b"*":
            self.mode = "marker"
            self.marker_buffer = bytearray()
        else:
            self.mode = "content"

    def _continue_line(self, segment: bytes) -> None:
        if self.mode == "marker":
            room = MAX_MARKER_LINE_BYTES + 1 - len(self.marker_buffer)
            self.marker_buffer += segment[:room]
            if len(self.marker_buffer) > MAX_MARKER_LINE_BYTES:
                if bytes(self.marker_buffer).startswith(b"*ATTACHEDFILE*"):
                    self._marker(self.marker_buffer, truncated=True)
                    self.mode = "skip_rest"
                else:
                    raise _Rejection(UnknownMarker(self.line_index))
            return
        if self.mode == "skip_rest":
            return
        if self.after_end:
            if segment.strip():
                raise _Rejection(ContentAfterEnd(self.line_index))
            return
        if self.keeping:
            self._keep(segment)

    def _end_line(self, final: bool = False) -> None:
        if self.mode == "marker":
            self._marker(self.marker_buffer, truncated=False)
        elif self.line_index == 0:
            raise _Rejection(SectionMissing(VERSION_PREFIX))
        elif self.mode == "content" and self.keeping and not self.after_end:
            self._keep(b"\n")
        self.mode = "content"
        self.at_line_start = True
        self.line_index += 1

    def _marker(self, raw: bytearray, truncated: bool) -> None:
        text = bytes(raw).rstrip(b"\r").decode("latin-1")
        stripped = text.strip()
        if self.after_end:
            raise _Rejection(ContentAfterEnd(self.line_index))
        if stripped == "*":
            if self.line_index == 0:
                raise _Rejection(SectionMissing(VERSION_PREFIX))
            if self.keeping:
                self._keep(bytes(raw) + b"\n")
            return
        if self.line_index == 0:
            if not text.startswith(VERSION_PREFIX):
                raise _Rejection(SectionMissing(VERSION_PREFIX))
            found = text[len(VERSION_PREFIX):].strip()
            if found != SUPPORTED_VERSION:
                raise _Rejection(UnsupportedVersion(found))
            self.version_seen = True
            self._keep(bytes(raw) + b"\n")
            self.keeping = False
            return
        if text.startswith(VERSION_PREFIX):
            raise _Rejection(SectionRepeated(VERSION_PREFIX))
        marker = "*ATTACHEDFILE*" if text.startswith("*ATTACHEDFILE*") else stripped
        if marker not in KNOWN_MARKERS:
            raise _Rejection(UnknownMarker(self.line_index))
        self._advance(marker)
        self.last_marker = marker
        self.keeping = marker in KEPT_MARKERS
        if self.keeping:
            self._keep(bytes(raw) + b"\n")
        if marker == "*END":
            self.after_end = True
            self.keeping = False
        if truncated:
            self.keeping = False

    def _advance(self, marker: str) -> None:
        if self.required_next < len(REQUIRED_ORDER):
            expected = REQUIRED_ORDER[self.required_next]
            if marker == expected:
                self.required_next += 1
                return
            if marker in REQUIRED_ORDER[:self.required_next]:
                raise _Rejection(SectionRepeated(marker))
            raise _Rejection(SectionOutOfOrder(marker, self.last_marker))
        if marker in REQUIRED_ORDER:
            raise _Rejection(SectionRepeated(marker))
        if marker in EXTRA_MARKERS:
            if self.tail_next > 0:
                raise _Rejection(SectionOutOfOrder(marker, self.last_marker))
            if marker == "*ATTACHEDFILE*":
                self.attachments += 1
                if self.attachments > self.limits.max_attachment_sections:
                    raise _Rejection(ExceedsLimit("max_attachment_sections"))
            return
        expected = TAIL_ORDER[self.tail_next] if self.tail_next < len(TAIL_ORDER) else None
        if marker == expected:
            self.tail_next += 1
            return
        if marker in TAIL_ORDER[:self.tail_next]:
            raise _Rejection(SectionRepeated(marker))
        if marker == "*END" and expected == "*PARTTYPELIST*":
            raise _Rejection(SectionMissing("*PARTTYPELIST*"))
        raise _Rejection(SectionOutOfOrder(marker, self.last_marker))


# ---------- extracting from the share ----------

def _content_addressed(destination_dir: pathlib.Path, share_file: pathlib.Path, file_hash: str) -> pathlib.Path:
    from cockpit.ingestion.service import content_addressed_path
    return content_addressed_path(destination_dir, share_file.with_suffix(EXTRACT_SUFFIX), file_hash)


def _hash_file(path: pathlib.Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK_BYTES), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _remove_quietly(path: pathlib.Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        logger.warning("A partial extract could not be removed; the startup sweep will: %s", describe_error(error))


def extract_bmb_sections(
    share_file: pathlib.Path,
    destination_dir: pathlib.Path,
    run_id: str,
    limits: BmbReadLimits = READ_LIMITS,
    share_call: ShareCallGuard = direct_share_call,
    stop: Any = None,
    progress: Callable[[int], None] | None = None,
) -> ExtractOutcome:
    """Reads a .bmb and stores only the sections Cockpit reads, content-addressed by the stored
    bytes' hash (Phase 57 §5.3)."""
    share_file = pathlib.Path(share_file)

    def stopped() -> bool:
        return stop is not None and stop.is_set()

    try:
        handle = share_call(lambda: open(share_file, "rb"))
    except OSError as error:
        return ShareReadFailed(describe_error(error))
    partial: pathlib.Path | None = None
    try:
        try:
            stat = share_call(lambda: os.fstat(handle.fileno()))
        except OSError as error:
            return ShareReadFailed(describe_error(error))
        if stat.st_size > limits.max_share_bytes:
            return Rejected(ExceedsLimit("max_share_bytes"))
        try:
            destination_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            return StorageFailed(describe_error(error))
        partial = destination_dir / f"{share_file.stem}.{run_id}{EXTRACT_SUFFIX}{PARTIAL_SUFFIX}"
        source_hasher = hashlib.sha256()
        bytes_read = 0
        try:
            out = open(partial, "wb")
        except OSError as error:
            partial = None
            return StorageFailed(describe_error(error))
        framer = _Framer(limits, out.write)
        outcome: ExtractOutcome | None = None
        try:
            while True:
                if stopped():
                    raise _StopRequested()
                try:
                    chunk = share_call(lambda: handle.read(CHUNK_BYTES))
                except OSError as error:
                    raise _ShareError(error)
                if not chunk:
                    break
                bytes_read += len(chunk)
                if bytes_read > limits.max_share_bytes:
                    raise _Rejection(ExceedsLimit("max_share_bytes"))
                source_hasher.update(chunk)
                framer.feed(chunk)
                if progress is not None:
                    progress(bytes_read)
            framer.finish()
        except _Rejection as rejection:
            outcome = Rejected(rejection.reason)
        except _StopRequested:
            outcome = Stopped()
        except _ShareError as failure:
            outcome = ShareReadFailed(describe_error(failure.error))
        except OSError as error:
            outcome = StorageFailed(describe_error(error))
        finally:
            out.close()
        if outcome is not None:
            return _fail(partial, outcome)

        file_hash = framer.kept_hasher.hexdigest()
        destination = _content_addressed(destination_dir, share_file, file_hash)
        extract = BmbExtract(
            stored_path=destination,
            file_hash=file_hash,
            source_hash=source_hasher.hexdigest(),
            source_size=stat.st_size,
            source_modified_ns=stat.st_mtime_ns,
            kept_bytes=framer.kept_bytes,
            skipped_bytes=bytes_read - framer.kept_bytes,
        )
        if stopped():
            return _fail(partial, Stopped())
        if destination.exists():
            try:
                existing = _hash_file(destination)
            except OSError as error:
                return _fail(partial, StorageFailed(describe_error(error)))
            _remove_quietly(partial)
            partial = None
            if existing != file_hash:
                return StorageFailed("STORED_COPY_MISMATCH")
            return Extracted(extract, moved_by_this_run=False)
        try:
            os.replace(partial, destination)
        except OSError as error:
            return _fail(partial, StorageFailed(describe_error(error)))
        partial = None
        return Extracted(extract, moved_by_this_run=True)
    finally:
        try:
            handle.close()
        except OSError:
            pass


class _StopRequested(Exception):
    pass


class _ShareError(Exception):
    def __init__(self, error: OSError) -> None:
        super().__init__(type(error).__name__)
        self.error = error


def _fail(partial: pathlib.Path | None, outcome: ExtractOutcome) -> ExtractOutcome:
    if partial is not None:
        _remove_quietly(partial)
    return outcome


# ---------- the stored extract ----------

def load_stored_extract(row: Any) -> BmbExtract | StoredExtractUnusable:
    """Opens a stored extract, checking its bytes against the row that names it (§5.3).

    pre:  row is a Placements source_files row
    post: the extract iff the file at row.local_storage_path exists, is readable, and hashes to
          row.file_hash
    """
    path = pathlib.Path(row.local_storage_path)
    if not path.is_file():
        return ExtractMissing()
    try:
        found = _hash_file(path)
        size = path.stat().st_size
    except OSError as error:
        return ExtractUnreadable(describe_error(error))
    if found != row.file_hash:
        return ExtractCorrupt(found)
    source_size = getattr(row, "source_size", None) or 0
    return BmbExtract(
        stored_path=path,
        file_hash=row.file_hash,
        source_hash=getattr(row, "source_hash", None) or row.file_hash,
        source_size=source_size,
        source_modified_ns=getattr(row, "source_modified_ns", None) or 0,
        kept_bytes=size,
        skipped_bytes=max(source_size - size, 0),
    )


# ---------- reading the project ----------

_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")
_INTEGER = re.compile(r"^-?\d+$")
_QUOTED = re.compile(r'\s*"([^"]*)"')
PRIMITIVES = frozenset({"V", "AP", "SR", "SC", "PG"})


@dataclass
class _Decal:
    outline_points: list[tuple[float, float]] = field(default_factory=list)
    pad_extents: list[tuple[float, float, float, float]] = field(default_factory=list)
    vertices: int = 0


def _bounding(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _body_of(decal: _Decal) -> Optional[BodyRectangle]:
    if decal.outline_points:
        x0, y0, x1, y1 = _bounding(decal.outline_points)
    elif decal.pad_extents:
        x0 = min(e[0] for e in decal.pad_extents)
        y0 = min(e[1] for e in decal.pad_extents)
        x1 = max(e[2] for e in decal.pad_extents)
        y1 = max(e[3] for e in decal.pad_extents)
    else:
        return None
    return BodyRectangle((x0 + x1) / 2.0, (y0 + y1) / 2.0, x1 - x0, y1 - y0)


def _is_header(tokens: list[str]) -> bool:
    return (
        len(tokens) == 8
        and tokens[0] not in PRIMITIVES
        and all(_NUMBER.match(t) for t in tokens[1:])
    )


def _primitives(tokens: list[str]) -> list[tuple[str, list[float]]]:
    found: list[tuple[str, list[float]]] = []
    for token in tokens:
        if token in PRIMITIVES:
            found.append((token, []))
        elif found and _NUMBER.match(token):
            found[-1][1].append(float(token))
    return found


def _add_primitive(decal: _Decal, keyword: str, numbers: list[float]) -> None:
    if keyword in ("V", "AP"):
        points = list(zip(numbers[1::2], numbers[2::2]))
        decal.vertices += len(points)
        if keyword == "V":
            decal.outline_points.extend(points)
    elif keyword == "SR" and len(numbers) >= 4:
        decal.vertices += 2
        x0, y0, x1, y1 = numbers[:4]
        decal.pad_extents.append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
    elif keyword == "SC" and len(numbers) >= 3:
        decal.vertices += 1
        x, y, diameter = numbers[:3]
        r = abs(diameter) / 2.0
        decal.pad_extents.append((x - r, y - r, x + r, y + r))
    elif keyword == "PG" and len(numbers) >= 2:
        points = list(zip(numbers[0::2], numbers[1::2]))
        decal.vertices += len(points)
        decal.pad_extents.append(_bounding(points))


def _parse_decals(lines: list[str], limits: BmbReadLimits) -> dict[str, _Decal]:
    decals: dict[str, _Decal] = {}
    current: _Decal | None = None
    for line in lines:
        tokens = line.split()
        if not tokens:
            continue
        if _is_header(tokens):
            if len(decals) >= limits.max_decals:
                raise _Rejection(ExceedsLimit("max_decals"))
            current = _Decal()
            decals.setdefault(tokens[0], current)
            continue
        if current is None:
            continue
        for keyword, numbers in _primitives(tokens):
            _add_primitive(current, keyword, numbers)
        if current.vertices > limits.max_decal_vertices:
            raise _Rejection(ExceedsLimit("max_decal_vertices"))
    return decals


@dataclass(frozen=True)
class _Record:
    designator: str
    placement: PlacementTriplet
    side: str
    is_stuffed: bool
    mount: str
    unplaced: bool
    part_number: str
    package_key: Optional[str]


def parse_bom_record(revealed: str, record_index: int) -> _Record:
    """Parses one revealed *BOM record (§5.2); a record that doesn't parse is RecordUnreadable."""
    if len(revealed) < DESIGNATOR_FIELD_CHARS:
        raise _Rejection(RecordUnreadable(record_index))
    designator = revealed[:DESIGNATOR_FIELD_CHARS].rstrip(" ")
    rest = revealed[DESIGNATOR_FIELD_CHARS:]
    head, quote, tail = rest.partition('"')
    tokens = head.split()
    if not designator or not quote or len(tokens) != 13 or not all(_INTEGER.match(t) for t in tokens[:6]):
        raise _Rejection(RecordUnreadable(record_index))
    side, stuffed, mount = tokens[6], tokens[7], tokens[8]
    marker = tokens[12]
    if side not in ("T", "B") or stuffed not in ("T", "F") or mount not in ("S", "T"):
        raise _Rejection(RecordUnreadable(record_index))
    quoted: list[str] = []
    remainder = '"' + tail
    for _ in range(5):
        match = _QUOTED.match(remainder)
        if match is None:
            raise _Rejection(RecordUnreadable(record_index))
        quoted.append(match.group(1))
        remainder = remainder[match.end():]
    part_number, _value, _rating, package_key, _decal = quoted
    package_key = package_key.strip()
    if len(package_key) > MAX_PACKAGE_KEY_CHARS:
        raise _Rejection(RecordUnreadable(record_index))
    return _Record(
        designator=designator,
        placement=PlacementTriplet(int(tokens[0]), int(tokens[1]), int(tokens[2])),
        side=side,
        is_stuffed=stuffed == "T",
        mount=mount,
        unplaced=marker.endswith("U"),
        part_number=part_number.strip(),
        package_key=None if package_key in ("", UNASSIGNED) else package_key,
    )


def _sections(lines: list[bytes]) -> dict[str, list[bytes]]:
    sections: dict[str, list[bytes]] = {}
    current = None
    for raw in lines[1:]:
        text = raw.decode("latin-1")
        if text.startswith("*") and text.strip() != "*":
            current = text.strip()
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(raw)
    return sections


def read_bmb_project(extract: BmbExtract, limits: BmbReadLimits = READ_LIMITS) -> BmbProject | BmbRejected:
    """Reads the placements and footprint bodies from a stored extract (§5.3)."""
    if reveal_bom_record is None:
        return DecoderUnavailable()
    data = pathlib.Path(extract.stored_path).read_bytes()
    lines = [line[:-1] if line.endswith(b"\r") else line for line in data.split(b"\n")]
    if not lines or not lines[0].startswith(VERSION_PREFIX.encode()):
        return SectionMissing(VERSION_PREFIX)
    version = lines[0][len(VERSION_PREFIX):].decode("latin-1").strip()
    if version != SUPPORTED_VERSION:
        return UnsupportedVersion(version)
    sections = _sections(lines)
    for required in ("*BOM", "*DECALS", "*PARTTYPES*"):
        if required not in sections:
            return SectionMissing(required)
    try:
        records_raw = [raw for raw in sections["*BOM"] if raw.strip()]
        if len(records_raw) > limits.max_records:
            return ExceedsLimit("max_records")
        decals = _parse_decals([raw.decode("latin-1") for raw in sections["*DECALS"]], limits)
        parttypes = []
        for raw in sections["*PARTTYPES*"]:
            text = raw.decode("latin-1").strip()
            if text and text != "*":
                parttypes.append(text.split()[0])
        records = [parse_bom_record(reveal_bom_record(raw, index), index)
                   for index, raw in enumerate(records_raw)]
    except _Rejection as rejection:
        return rejection.reason

    for index, record in enumerate(records):
        if index >= len(parttypes) or parttypes[index] != record.designator:
            return DecodeCheckFailed(index)
    if len(parttypes) != len(records):
        return DecodeCheckFailed(len(records))
    seen: set[str] = set()
    for index, record in enumerate(records):
        if record.designator in seen:
            return DuplicateDesignator(index)
        seen.add(record.designator)

    placements: list[BmbPlacement] = []
    unplaced: set[str] = set()
    for record in records:
        if record.unplaced:
            unplaced.add(record.designator)
            continue
        decal = decals.get(record.package_key) if record.package_key else None
        placements.append(BmbPlacement(
            designator=record.designator,
            placement=record.placement,
            side=record.side,
            is_stuffed=record.is_stuffed,
            mount=record.mount,
            part_number=record.part_number,
            package_key=record.package_key,
            body=_body_of(decal) if decal is not None else None,
        ))
    return BmbProject(version, tuple(placements), frozenset(unplaced))


# ---------- rejection reasons as stored JSON ----------

def rejection_to_json(reason: Any) -> dict[str, Any]:
    payload = {"kind": type(reason).__name__}
    payload.update({k: v for k, v in vars(reason).items()} if hasattr(reason, "__dict__") else {})
    return payload


def rejection_from_json(payload: dict[str, Any]) -> Any:
    kind = payload.get("kind", "")
    cls = REJECTION_TYPES.get(kind)
    if cls is None:
        from cockpit.placements.admission import ADMISSION_TYPES
        cls = ADMISSION_TYPES.get(kind)
    if cls is None:
        return UnrecognisedReason(kind)
    fields = {k: v for k, v in payload.items() if k != "kind"}
    try:
        return cls(**fields)
    except TypeError:
        return UnrecognisedReason(kind)


@dataclass(frozen=True)
class UnrecognisedReason:
    kind: str
