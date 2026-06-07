from dataclasses import dataclass
from dataclasses import dataclass
from typing import Dict, Optional
from datetime import date
from cockpit.persistence.types import AuditStatus
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
    floor_notes: str
    shortages_notes: str
    pcb_clear: str
    setup_first_side: str
    program_in_kit: bool
    folder_in_kit: bool

class ReleaseService:
    def __init__(self, audit_repo: AuditRepository):
        self._audit_repo = audit_repo

    def persist_release(self, audit_id: int, workflow_status: AuditStatus, ship_date: date | None) -> None:
        from cockpit.persistence.errors import AuditNotFound
        from cockpit.persistence.clock import utcnow
        
        ship_str = ship_date.isoformat() if ship_date else None
        now_iso = utcnow().isoformat()
        
        cur = self._audit_repo.conn.cursor()
        cur.execute(
            "UPDATE active_audits SET status = ?, ship_date = ?, updated_at = ? WHERE id = ?",
            (workflow_status.value, ship_str, now_iso, audit_id)
        )
        if cur.rowcount == 0:
            raise AuditNotFound(audit_id)
        
    def build_defaults(self, view: ActiveAuditView) -> ReleaseFormData:
        meta = view.traveler_metadata or {}
        
        itar_classification = meta.get("itar_classification", "")
        itar_display = "ITAR" if str(itar_classification).strip().upper() == "YES" else ""
        
        assembly_class = meta.get("assembly_class")
        class_display = f"Class {assembly_class}" if assembly_class is not None else ""
        
        from cockpit.services.repeat import derive_repeat
        return ReleaseFormData(
            assembly_number=meta.get("assembly_number"),
            quantity=view.quantity,
            lead_time_days=meta.get("lead_time_days"),
            repeat=derive_repeat(meta),
            itar_display=itar_display,
            process_clean=meta.get("process_clean"),
            class_display=class_display,
            process=meta.get("process"),
            ship_date="",
            turn_note="",
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
        doc.setDefaultFont(QFont("Calibri", 18))
        
        html_content = f"""
        <h1>JOB RELEASE FORM</h1>
        <table border="1" width="100%" cellspacing="0" cellpadding="5">
            <tr><td><b>Assembly Number:</b> {html.escape(str(data.assembly_number or ''))}</td>
                <td><b>Quantity:</b> {html.escape(str(data.quantity or ''))}</td></tr>
            <tr><td><b>Lead Time:</b> {html.escape(str(data.lead_time_days or ''))} days</td>
                <td><b>Type:</b> {html.escape(data.repeat)}</td></tr>
            <tr><td><b>ITAR:</b> {html.escape(data.itar_display)}</td>
                <td>{html.escape(data.class_display)}</td> </td></tr>
            <tr><td><b>Process:</b> {html.escape(str(data.process or ''))} {html.escape(str(data.process_clean or ''))}</td></tr>
        </table>
        <br>
        <p><b>HOT JOB:</b> {html.escape(data.turn_note)}</p>
        <p><b>Ship Date:</b> {html.escape(data.ship_date)}</p>
        <br>
        <p><b>Setup First Side:</b> {html.escape(data.setup_first_side)}</p>
        <br>
        <p><b>PCB Clear:</b> {html.escape(data.pcb_clear)}</p>
        <p><b>Shortages Notes:</b> {html.escape(data.shortages_notes)}</p>
        <p><b>Program In Kit:</b> {'Yes' if data.program_in_kit else 'No'}</p>
        <p><b>Folder In Kit:</b> {'Yes' if data.folder_in_kit else 'No'}</p>
        <p><b>Floor Notes:</b> {html.escape(data.floor_notes)}</p>
        """
        doc.setHtml(html_content)
        
        try:
            doc.print(printer)
        except Exception as e:
            from cockpit.services.errors import PrintError
            raise PrintError(f"Failed to print release form: {str(e)}", {"error": str(e)})
