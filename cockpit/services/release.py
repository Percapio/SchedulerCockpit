from dataclasses import dataclass
from typing import Dict, Optional
from cockpit.services.views import ActiveAuditView
from cockpit.persistence.repositories.audits import AuditRepository
from PyQt6.QtPrintSupport import QPrinter

@dataclass(frozen=True)
class ReleaseFormData:
    assembly_number: str | None
    quantity: int | None
    lead_time_days: int | None
    repeat: str
    itar_display: str
    process_clean: str | None
    class_display: str
    process: str | None
    ship_date: str
    turn_note: str
    email_notes: str
    floor_notes: str
    shortages_notes: str
    pcb_clear: str
    setup_first_side: str
    program_in_kit: bool
    folder_in_kit: bool

class ReleaseService:
    def __init__(self, audit_repo: AuditRepository):
        self._audit_repo = audit_repo

    def transition_status(self, audit_id: int, new_status: str) -> None:
        from cockpit.persistence.types import AuditStatus
        status_enum = AuditStatus(new_status)
        self._audit_repo.transition_status(audit_id, status_enum)

    def derive_repeat(self, meta: dict) -> str:
        if meta.get("assembly_type") == "NEW":
            return "NEW"
        parts = [
            meta.get("rowc_label", ""),
            meta.get("rowc_ref", ""),
            meta.get("assembly_modifier", "")
        ]
        return " ".join(p for p in parts if p)
        
    def build_defaults(self, view: ActiveAuditView) -> ReleaseFormData:
        meta = view.traveler_metadata or {}
        
        itar_classification = meta.get("itar_classification", "")
        itar_display = "ITAR" if str(itar_classification).strip().upper() == "YES" else ""
        
        assembly_class = meta.get("assembly_class")
        class_display = f"Class {assembly_class}" if assembly_class is not None else ""
        
        return ReleaseFormData(
            assembly_number=meta.get("assembly_number"),
            quantity=view.quantity,
            lead_time_days=meta.get("lead_time_days"),
            repeat=self.derive_repeat(meta),
            itar_display=itar_display,
            process_clean=meta.get("process_clean"),
            class_display=class_display,
            process=meta.get("process"),
            ship_date="",
            turn_note="",
            email_notes="",
            floor_notes="",
            shortages_notes="",
            pcb_clear="",
            setup_first_side="",
            program_in_kit=False,
            folder_in_kit=False
        )

    def print_release_form(self, data: ReleaseFormData, printer: QPrinter) -> None:
        from PyQt6.QtGui import QTextDocument, QFont
        import html
        
        doc = QTextDocument()
        doc.setDefaultFont(QFont("Arial", 12))
        
        html_content = f"""
        <h1>JOB RELEASE FORM</h1>
        <table border="1" width="100%" cellspacing="0" cellpadding="5">
            <tr><td><b>Assembly Number:</b> {html.escape(str(data.assembly_number or ''))}</td>
                <td><b>Quantity:</b> {html.escape(str(data.quantity or ''))}</td></tr>
            <tr><td><b>Lead Time:</b> {html.escape(str(data.lead_time_days or ''))} days</td>
                <td><b>Repeat:</b> {html.escape(data.repeat)}</td></tr>
            <tr><td><b>ITAR:</b> {html.escape(data.itar_display)}</td>
                <td><b>Clean:</b> {html.escape(str(data.process_clean or ''))}</td></tr>
            <tr><td><b>Class:</b> {html.escape(data.class_display)}</td>
                <td><b>Process:</b> {html.escape(str(data.process or ''))}</td></tr>
        </table>
        <h2>Manual Notes</h2>
        <p><b>Ship Date:</b> {html.escape(data.ship_date)}</p>
        <p><b>Turn Note:</b> {html.escape(data.turn_note)}</p>
        <p><b>Email Notes:</b> {html.escape(data.email_notes)}</p>
        <p><b>Floor Notes:</b> {html.escape(data.floor_notes)}</p>
        <p><b>Shortages Notes:</b> {html.escape(data.shortages_notes)}</p>
        <p><b>PCB Clear:</b> {html.escape(data.pcb_clear)}</p>
        <p><b>Setup First Side:</b> {html.escape(data.setup_first_side)}</p>
        <p><b>Program In Kit:</b> {'Yes' if data.program_in_kit else 'No'}</p>
        <p><b>Folder In Kit:</b> {'Yes' if data.folder_in_kit else 'No'}</p>
        """
        doc.setHtml(html_content)
        
        try:
            doc.print(printer)
        except Exception as e:
            from cockpit.services.errors import PrintError
            raise PrintError(f"Failed to print release form: {str(e)}", {"error": str(e)})
