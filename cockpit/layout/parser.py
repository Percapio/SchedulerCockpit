"""PDF Layout Parser."""

import pathlib
from typing import Iterable

import fitz  # PyMuPDF

from cockpit.ingestion.errors import MalformedPdfError
from cockpit.layout.pdf_access import PdfStep, pdf_section
from .results import PdfComponentCoordinate, PdfLayoutResult
from .splitter import looks_merged, split_merged_label, target_lengths


class PdfLayoutParser:
    def parse(
        self,
        path: pathlib.Path,
        target_ref_des: set[str],
        split_targets: Iterable[str] | None = None,
    ) -> PdfLayoutResult:
        """Finds each target designator's label on a drawing (Phase 57 §4).

        Pass 1 is the shipped whole-span match, unchanged: a span whose stripped text is a target
        gives that designator's label, first occurrence wins. Pass 2 runs only on pages where pass
        1 saw a merged span, and splits it against split_targets (the targets plus the BOM's DNI
        designators; the targets alone when absent), adding a from_split label for each target
        neither pass has found. DNI designators are never returned.
        """
        keep = set(target_ref_des)
        split = set(split_targets) if split_targets is not None else set(keep)
        split |= keep
        lengths = target_lengths(split)

        # Patch 14 §9 (INV-P1): every fitz touch is inside a pdf_section, and every fitz reference is
        # released inside the section that made it; the Python work on what a step returned runs outside.
        with pdf_section(PdfStep.OPEN):
            try:
                doc = fitz.open(str(path))
            except Exception as e:
                raise MalformedPdfError(path, "UNREADABLE_PDF", {"error": str(e)})
            try:
                page_count = len(doc)
            except BaseException:
                doc.close()
                doc = None
                raise

        coordinates: list[PdfComponentCoordinate] = []
        found_ref_des: set[str] = set()
        total_spans = 0
        flagged_pages: list[int] = []

        try:
            for page_index in range(page_count):
                with pdf_section(PdfStep.PAGE_TEXT):
                    page = doc[page_index]
                    try:
                        text_dict = page.get_text("dict")
                    finally:
                        page = None
                page_flagged = False

                blocks = text_dict.get("blocks", [])
                for block in blocks:
                    if block.get("type") != 0:
                        continue  # Not a text block

                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            total_spans += 1
                            text = span.get("text", "").strip()

                            if text in keep and text not in found_ref_des:
                                bbox = span.get("bbox")
                                if bbox and len(bbox) == 4:
                                    x0, y0, x1, y1 = bbox

                                    # PyMuPDF bbox is (x0, y0, x1, y1)
                                    # Normalize coordinates to ensure x1 <= x2 and y1 <= y2
                                    nx1 = min(x0, x1)
                                    ny1 = min(y0, y1)
                                    nx2 = max(x0, x1)
                                    ny2 = max(y0, y1)

                                    coordinates.append(PdfComponentCoordinate(
                                        ref_des=text,
                                        page_index=page_index,
                                        x1=nx1,
                                        y1=ny1,
                                        x2=nx2,
                                        y2=ny2
                                    ))
                                    found_ref_des.add(text)
                            elif not page_flagged and lengths and looks_merged(text, split, lengths):
                                page_flagged = True

                if page_flagged:
                    flagged_pages.append(page_index)

            if total_spans == 0 and page_count > 0:
                # No text spans found across the entire document
                raise MalformedPdfError(path, "RASTER_PDF", {})

            for page_index in flagged_pages:
                with pdf_section(PdfStep.PAGE_RAWTEXT):
                    page = doc[page_index]
                    try:
                        raw = page.get_text("rawdict")
                    finally:
                        page = None
                self._split_merged_spans(raw, page_index, keep, split, lengths,
                                         coordinates, found_ref_des)

            return PdfLayoutResult(
                coordinates=coordinates,
                found_ref_des=found_ref_des,
                page_count=page_count
            )
        finally:
            with pdf_section(PdfStep.CLOSE):
                try:
                    doc.close()
                finally:
                    doc = None

    @staticmethod
    def _split_merged_spans(
        raw: dict,
        page_index: int,
        keep: set[str],
        split: set[str],
        lengths: tuple[int, ...],
        coordinates: list[PdfComponentCoordinate],
        found_ref_des: set[str],
    ) -> None:
        """Splits merged spans in one page's rawdict, outside the PyMuPDF lock (Patch 14 §9.2)."""
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    chars = span.get("chars", [])
                    # A glyph that maps to several characters (a ligature) breaks the one-box-per-
                    # character alignment the splitter needs: leave such a span alone.
                    if not chars or any(len(c.get("c", "")) != 1 for c in chars):
                        continue
                    text = "".join(c["c"] for c in chars)
                    stripped = text.strip()
                    if not stripped or stripped in split:
                        continue
                    if not looks_merged(stripped, split, lengths):
                        continue
                    lead = len(text) - len(text.lstrip())
                    boxes = [c["bbox"] for c in chars[lead:lead + len(stripped)]]
                    labels = split_merged_label(stripped, boxes, split, lengths)
                    if not isinstance(labels, list):
                        continue
                    for label in labels:
                        designator = label.designator
                        if designator not in keep or designator in found_ref_des:
                            continue
                        x0, y0, x1, y1 = label.box
                        coordinates.append(PdfComponentCoordinate(
                            ref_des=designator,
                            page_index=page_index,
                            x1=min(x0, x1),
                            y1=min(y0, y1),
                            x2=max(x0, x1),
                            y2=max(y0, y1),
                            from_split=True,
                        ))
                        found_ref_des.add(designator)
