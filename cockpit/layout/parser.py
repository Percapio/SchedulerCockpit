"""PDF Layout Parser."""

import pathlib
import fitz  # PyMuPDF

from cockpit.ingestion.errors import MalformedPdfError
from .results import PdfComponentCoordinate, PdfLayoutResult


class PdfLayoutParser:
    def parse(self, path: pathlib.Path, target_ref_des: set[str]) -> PdfLayoutResult:
        """Parse PDF text spans to locate bounding boxes for target RefDes."""
        try:
            doc = fitz.open(str(path))
        except Exception as e:
            raise MalformedPdfError(path, "UNREADABLE_PDF", {"error": str(e)})

        coordinates: list[PdfComponentCoordinate] = []
        found_ref_des: set[str] = set()
        total_spans = 0

        try:
            for page_index in range(len(doc)):
                page = doc[page_index]
                text_dict = page.get_text("dict")
                
                blocks = text_dict.get("blocks", [])
                for block in blocks:
                    if block.get("type") != 0:
                        continue  # Not a text block
                        
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            total_spans += 1
                            text = span.get("text", "").strip()
                            
                            if text in target_ref_des and text not in found_ref_des:
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
                                    
            if total_spans == 0 and len(doc) > 0:
                # No text spans found across the entire document
                raise MalformedPdfError(path, "RASTER_PDF", {})
                
            return PdfLayoutResult(
                coordinates=coordinates,
                found_ref_des=found_ref_des,
                page_count=len(doc)
            )
        finally:
            doc.close()
