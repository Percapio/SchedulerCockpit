"""Service for layout and rendering queries."""

from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.types import SourceFileCategory
from cockpit.layout.renderer import PdfRenderer
from cockpit.services.views import LayoutContext


class LayoutQueryService:
    def __init__(
        self,
        source_file_repo: SourceFileRepository,
        pdf_renderer: PdfRenderer,
    ) -> None:
        self.source_file_repo = source_file_repo
        self.pdf_renderer = pdf_renderer

    def load_for_audit(self, audit_id: int) -> LayoutContext:
        """Return the canvas's context for one audit."""
        source_file = self.source_file_repo.find_by_audit_and_category(
            audit_id, SourceFileCategory.PDF
        )

        if source_file is None:
            return LayoutContext(
                audit_id=audit_id,
                pdf_source_file_id=None,
                pdf_path=None,
                page_count=0,
                page_dimensions=()
            )

        pdf_path = source_file.local_storage_path
        dimensions = self.pdf_renderer.get_page_dimensions(pdf_path)

        return LayoutContext(
            audit_id=audit_id,
            pdf_source_file_id=source_file.id,
            pdf_path=pdf_path,
            page_count=len(dimensions),
            page_dimensions=dimensions
        )
