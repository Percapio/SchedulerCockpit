"""Splitting a text span that runs several designators together (Phase 57 §4)."""

from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Sequence

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class SplitLabel:
    designator: str
    box: Box  # x0, y0, x1, y1, page points


@dataclass(frozen=True)
class NoCover:
    pass


@dataclass(frozen=True)
class AmbiguousCover:
    covers: int


LabelNotSplit = NoCover | AmbiguousCover


def target_lengths(targets: Iterable[str]) -> tuple[int, ...]:
    """The distinct lengths of a target set, longest first."""
    return tuple(sorted({len(t) for t in targets if t}, reverse=True))


def looks_merged(text: str, targets: set[str], lengths: Sequence[int]) -> bool:
    """Whether a span is a merged-label candidate: its text, whitespace removed, starts with a
    target and is longer than that target."""
    compact = "".join(text.split())
    return any(len(compact) > n and compact[:n] in targets for n in lengths)


def _union(boxes: Sequence[Sequence[float]]) -> Box:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def split_merged_label(
    span_text: str,
    character_boxes: Sequence[Sequence[float]],
    targets: set[str],
    lengths: Sequence[int] | None = None,
) -> list[SplitLabel] | LabelNotSplit:
    """Splits a span that spells several targets, separated only by whitespace (Phase 57 §4).

    pre:  span_text is not itself a member of targets; character_boxes align one to one with
          span_text's characters
    post: the labels iff exactly one sequence of targets spells span_text; each box is the union of
          that designator's character boxes; NoCover or AmbiguousCover otherwise
    """
    text = span_text
    if len(character_boxes) != len(text) or not text or text != text.strip():
        return NoCover()
    sizes = tuple(lengths) if lengths is not None else target_lengths(targets)
    end = len(text)

    @lru_cache(maxsize=None)
    def covers_from(i: int) -> int:
        count = 0
        for n in sizes:
            j = i + n
            if j > end or text[i:j] not in targets:
                continue
            k = j
            while k < end and text[k].isspace():
                k += 1
            count += 1 if k == end else covers_from(k)
        return count

    total = covers_from(0)
    if total == 0:
        return NoCover()
    if total > 1:
        return AmbiguousCover(total)

    labels: list[SplitLabel] = []
    i = 0
    while i < end:
        for n in sizes:
            j = i + n
            if j > end or text[i:j] not in targets:
                continue
            k = j
            while k < end and text[k].isspace():
                k += 1
            if k == end or covers_from(k) == 1:
                labels.append(SplitLabel(text[i:j], _union(character_boxes[i:j])))
                i = k
                break
        else:  # pragma: no cover - a unique cover always continues
            return NoCover()
    covers_from.cache_clear()
    return labels
