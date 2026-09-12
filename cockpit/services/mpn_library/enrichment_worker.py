import sqlite3
from typing import Callable
from datetime import datetime
from pathlib import Path
from PyQt6.QtCore import QThread, pyqtSignal

from .types import MpnKey
from .digikey_types import (
    DigiKeyCredentials,
    RequestBudget,
    Resolved,
    Ambiguous,
    NotFound
)
from .enrichment_plan import EnrichmentPlan, EnrichmentReport, FailureReason, AbortCause
from .digikey_gateway import DigiKeyGateway
from cockpit.services.library_errors import (
    CredentialsRejected,
    QuotaExhausted,
    DigiKeyUnreachable,
    MalformedCatalogueResponse
)
from .connection import LibraryConnection

class EnrichmentWorker(QThread):
    part_enriched = pyqtSignal(str, str) # MpnKey, ResolutionStatus
    finished_report = pyqtSignal(object) # EnrichmentReport (using finished_report to not collide with QThread.finished)
    failed = pyqtSignal(object)

    def __init__(
        self,
        plan: EnrichmentPlan,
        credentials: DigiKeyCredentials,
        budget: RequestBudget,
        library_path: Path,
        utcnow: Callable[[], datetime]
    ):
        super().__init__()
        self.plan = plan
        self.credentials = credentials
        self.budget = budget
        self.library_path = library_path
        self.utcnow = utcnow
        
        self.report = EnrichmentReport()
        self.report.unqueried = list(self.plan.to_query) # start with all unqueried
        self._is_cancelled = False
        
    def cancel(self):
        self._is_cancelled = True
        
    def run(self):
        try:
            worker_conn = LibraryConnection(self.library_path)
        except Exception as e:
            self.failed.emit(e)
            return
            
        gateway = DigiKeyGateway()
        gateway.initialize(self.credentials)
        
        try:
            for mpn_key in self.plan.to_query:
                if self._is_cancelled:
                    self.report.abort_cause = AbortCause.CANCELLED
                    break
                    
                self.report.unqueried.remove(mpn_key)
                
                try:
                    result = gateway.resolve_mpn(mpn_key, self.budget, lambda: self.utcnow())
                    
                    if isinstance(result, Resolved):
                        status = "RESOLVED"
                        self.report.resolved.append(mpn_key)
                    elif isinstance(result, Ambiguous):
                        status = "AMBIGUOUS"
                        self.report.ambiguous.append(mpn_key)
                    else:
                        status = "NOT_FOUND"
                        self.report.not_found.append(mpn_key)
                        
                    self._commit_part(worker_conn, mpn_key, status, result)
                    self.part_enriched.emit(mpn_key, status)
                    
                except CredentialsRejected:
                    self.report.abort_cause = AbortCause.CREDENTIALS
                    self.report.unqueried.append(mpn_key)
                    break
                except QuotaExhausted:
                    self.report.abort_cause = AbortCause.QUOTA
                    self.report.unqueried.append(mpn_key)
                    break
                except DigiKeyUnreachable:
                    self.report.failed.append((mpn_key, FailureReason.SERVER_ERROR)) # or TRANSPORT_FAILED
                except MalformedCatalogueResponse:
                    self.report.failed.append((mpn_key, FailureReason.MALFORMED_RESPONSE))
                except Exception as e:
                    # Generic error per-part
                    if type(e).__name__ == "RequestRejected":
                        self.report.failed.append((mpn_key, FailureReason.REQUEST_REJECTED))
                    else:
                        self.report.failed.append((mpn_key, FailureReason.TRANSPORT_FAILED))
                        
            # Record quota spend
            self.report.requests_spent = self.budget.spent
            self._record_quota_spend(worker_conn, self.budget.spent)
            
            self.finished_report.emit(self.report)
            
        except Exception as e:
            self.failed.emit(e)
        finally:
            worker_conn.close()

    def _commit_part(self, conn: LibraryConnection, mpn_key: MpnKey, status: str, result):
        now_iso = self.utcnow().isoformat()
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            # Update library_part status
            cur.execute(
                """
                UPDATE library_part 
                SET resolution_status = ?, last_attempted_at = ?, last_resolved_at = ? 
                WHERE mpn_key = ?
                """,
                (status, now_iso, now_iso if status != "NOT_FOUND" else None, mpn_key)
            )
            
            # Record snapshot
            # pruning to SNAPSHOT_RETENTION_COUNT (say 5)
            # We don't have SNAPSHOT_RETENTION_COUNT defined, default to 5
            SNAPSHOT_RETENTION_COUNT = 5
            
            cur.execute(
                """
                INSERT INTO digikey_snapshot (mpn_key, fetched_at, http_status, payload_json, payload_bytes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (mpn_key, now_iso, result.http_status, result.raw_response.decode('utf-8', errors='ignore'), len(result.raw_response))
            )
            
            # Prune snapshots
            cur.execute(
                """
                DELETE FROM digikey_snapshot 
                WHERE mpn_key = ? AND fetched_at NOT IN (
                    SELECT fetched_at FROM digikey_snapshot 
                    WHERE mpn_key = ? 
                    ORDER BY fetched_at DESC 
                    LIMIT ?
                )
                """,
                (mpn_key, mpn_key, SNAPSHOT_RETENTION_COUNT)
            )
            
            # Write attributes if RESOLVED
            if status == "RESOLVED":
                from .types import Provenance
                for attr in result.candidate.attributes:
                    # Upsert (update in place or insert)
                    cur.execute(
                        """
                        SELECT value_text, value_numeric, confirmed_at 
                        FROM part_attribute 
                        WHERE mpn_key = ? AND attribute_name = ? AND provenance = ?
                        """,
                        (mpn_key, attr.attribute_name, Provenance.DIGIKEY.value)
                    )
                    row = cur.fetchone()
                    if row:
                        if row["value_text"] != attr.value_text or row["value_numeric"] != attr.value_numeric:
                            confirmed_at = None
                        else:
                            confirmed_at = row["confirmed_at"]
                            
                        cur.execute(
                            """
                            UPDATE part_attribute 
                            SET value_text = ?, value_numeric = ?, confirmed_at = ? 
                            WHERE mpn_key = ? AND attribute_name = ? AND provenance = ?
                            """,
                            (attr.value_text, attr.value_numeric, confirmed_at, mpn_key, attr.attribute_name, Provenance.DIGIKEY.value)
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO part_attribute 
                            (mpn_key, attribute_name, provenance, value_text, value_numeric, source_label, recorded_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (mpn_key, attr.attribute_name, Provenance.DIGIKEY.value, attr.value_text, attr.value_numeric, attr.source_label, now_iso)
                        )
                        
            conn.commit()
            
            # Check for unmapped packaging labels
            if hasattr(result, 'candidate') and result.candidate.packaging_type.value == "UNKNOWN":
                if result.candidate.packaging_label not in self.report.unmapped_packaging_labels:
                    self.report.unmapped_packaging_labels.append(result.candidate.packaging_label)
            elif hasattr(result, 'candidates'):
                for c in result.candidates:
                    if c.packaging_type.value == "UNKNOWN" and c.packaging_label not in self.report.unmapped_packaging_labels:
                        self.report.unmapped_packaging_labels.append(c.packaging_label)
                        
        except Exception:
            conn.rollback()
            self.report.failed.append((mpn_key, FailureReason.SNAPSHOT_WRITE_FAILED))
            
    def _record_quota_spend(self, conn: LibraryConnection, spent: int):
        if spent == 0:
            return
            
        today = self.utcnow().strftime("%Y-%m-%d")
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute(
                "INSERT INTO library_quota_day (quota_date, calls_made) VALUES (?, ?) "
                "ON CONFLICT(quota_date) DO UPDATE SET calls_made = calls_made + excluded.calls_made",
                (today, spent)
            )
            conn.commit()
        except Exception:
            conn.rollback()
