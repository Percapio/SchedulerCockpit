"""Ingestion service orchestrator."""

import logging
import pathlib
import shutil
import sqlite3
import time
import dataclasses
from dataclasses import dataclass
from typing import Sequence, Callable, Any

from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.repositories.tht_checklist import ThtChecklistRepository
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from cockpit.persistence.types import (
    ActiveAudit, AuditBomComponentDraft, PdfComponentCoordDraft,
    SourceFileCategory, SourceFileDraft, ThtChecklistItemDraft
)

from . import categorizer
from . import cross_validation
from . import gatekeeper
from . import hashing
from .errors import FileStorageError, ProbedIdentityMismatch, StoredFileVerificationError
from .filename_rules import derive_part_number_from_filename
from .parsers import audit_bom, coordinate_map, eco_build_notes, traveler
from .progress import ProgressEvent, ProgressStage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbedIdentity:
    """The identity a candidate ingest will claim, read before anything is written.

    post: part_number and work_order_ref match what reconcile derives from the
          same traveler; quantity is the document's own figure, before any
          operator override; traveler_hash pins the bytes that were read
    """

    part_number: str
    work_order_ref: str
    quantity: int
    traveler_hash: str


@dataclass(frozen=True)
class IngestionPlan:
    """Everything the pre-flight settled, carried into the ingest as one value.

    When absent, ingest() behaves exactly as it did before Phase 51: no identity
    assertion, no family deleted, FA recorded, no override. That is why every
    existing call site and test is unaffected.
    """

    expected_identity: ProbedIdentity | None = None
    replace_family: tuple[int, ...] = ()
    article_revision: str = "FA"
    quantity_override: int | None = None


def probe_identity(
    traveler_path: pathlib.Path,
    coord_map: coordinate_map.TravelerCoordinateMap,
) -> ProbedIdentity:
    """Reads the identity a file set will claim, without copying or committing.

    pre:  traveler_path is readable
    post: nothing is written anywhere; the returned identity is what reconcile
          will derive from the same file
    raises: the traveler parser's own errors; CrossValidationError when the
            traveler names no work order or no usable quantity

    Only the traveler is parsed. The BOM and ECO contribute nothing to identity,
    so parsing them here would double the pre-flight's cost for no answer.
    """
    from .errors import CrossValidationError

    result = traveler.parse(traveler_path, coord_map)
    mapping = coord_map.identity_mapping
    fields = result.extracted_fields

    part_number = str(fields.get(mapping.part_number_field) or "").strip()
    if not part_number:
        raise CrossValidationError("PART_NUMBER_MISSING", {"field": mapping.part_number_field})

    work_order = str(fields.get(mapping.work_order_ref_field) or "").strip()
    if not work_order:
        raise CrossValidationError("WORK_ORDER_MISSING", {"field": mapping.work_order_ref_field})

    quantity = fields.get(mapping.quantity_field)
    if not isinstance(quantity, int) or quantity < 1:
        raise CrossValidationError(
            "QUANTITY_INVALID", {"value": quantity, "field": mapping.quantity_field}
        )

    return ProbedIdentity(
        part_number=part_number,
        work_order_ref=work_order,
        quantity=quantity,
        traveler_hash=hashing.sha256_hex(traveler_path),
    )


def content_addressed_path(
    audit_dir: pathlib.Path, source: pathlib.Path, source_hash: str
) -> pathlib.Path:
    """Where a source file is stored, keyed by its content.

    post: two files with the same name and different bytes resolve to different
          paths; the same bytes always resolve to the same path

    Twelve hex characters of SHA-256. The prefix only has to keep coexisting
    revisions apart in one directory; the full hash is what reference_count and
    the orphan sweep match on, and the post-copy verification is what makes the
    truncation safe.
    """
    return audit_dir / f"{source.stem}__{source_hash[:12]}{source.suffix}"



class IngestionService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        audit_repo: AuditRepository,
        source_file_repo: SourceFileRepository,
        tht_repo: ThtChecklistRepository,
        bom_component_repo: AuditBomComponentRepository,
        pdf_coord_repo: PdfComponentCoordRepository,
        layout_parser: Any,
        coord_map: coordinate_map.TravelerCoordinateMap,
        file_storage_root: pathlib.Path,
        runtime_calc_svc = None
    ) -> None:
        self.conn = conn
        self.audit_repo = audit_repo
        self.source_file_repo = source_file_repo
        self.tht_repo = tht_repo
        self.bom_component_repo = bom_component_repo
        self.pdf_coord_repo = pdf_coord_repo
        self.layout_parser = layout_parser
        self.coord_map = coord_map
        self.file_storage_root = file_storage_root
        self._runtime_calc_svc = runtime_calc_svc

    def _assert_matches_probe(self, plan, intent, trav_hash: str) -> None:
        """Refuses an ingest whose identity drifted from what was confirmed.

        pre:  called after reconcile and before the savepoint
        post: returns silently when no plan carried an identity, or when the
              authoritative parse agrees with it
        raises: ProbedIdentityMismatch, so the copy rollback runs and the
                family the operator agreed to replace is left intact
        """
        expected = plan.expected_identity
        if expected is None:
            return

        draft = intent.audit_draft
        if (
            draft.part_number != expected.part_number
            or draft.work_order_ref != expected.work_order_ref
            or trav_hash != expected.traveler_hash
        ):
            raise ProbedIdentityMismatch(
                expected=(expected.part_number, expected.work_order_ref),
                found=(draft.part_number, draft.work_order_ref),
            )

    def apply_quantity_override(self, audit_id: int, override_quantity: int) -> None:
        """Applies an operator-entered quantity to a freshly ingested audit.

        pre:  audit was created in the caller's transaction; override_quantity >= 1
        post: active_audits.quantity == override_quantity; traveler_metadata
              retains the parsed value byte-identically, because the premise of
              the override is that the document is wrong and overwriting the
              metadata would destroy the evidence of what it said

        The runtime recompute is the caller's, via the persist already inside
        the savepoint: every stage runtime is quantity-derived, so applying the
        override after that persist would leave them computed against the stale
        figure.
        """
        from ..persistence.errors import InvalidArgumentError

        if override_quantity < 1:
            raise InvalidArgumentError("override_quantity", override_quantity, "Must be >= 1")
        self.audit_repo.set_quantity(audit_id, override_quantity)

    def _unlink_copies(self, copied_paths: list[pathlib.Path]) -> None:
        """Removes files this call created, and only those.

        Phase 45 section 4.2's guard, which add_pdf_to_audit already carried and
        ingest() did not: without the reference_count clause a failed re-ingest
        of an already-stored job deletes files other rows -- including sibling
        audits' -- still reference. Same-named destinations used to make that
        unreachable; content addressing removes the accident, so the guard
        becomes load-bearing.
        """
        for path in copied_paths:
            try:
                if self.source_file_repo.reference_count(hashing.sha256_hex(path)) == 0:
                    path.unlink()
            except Exception:
                logger.debug("Could not remove %s during rollback", path, exc_info=True)

    def ingest(
        self,
        paths: Sequence[pathlib.Path],
        progress: Callable[[ProgressEvent], None] | None = None,
        *,
        plan: IngestionPlan | None = None,
    ) -> ActiveAudit:
        """Ingest a dropped trio, atomically persist, and return the new audit.

        `plan` carries what the pre-flight settled: the identity to assert, the
        family to replace, the article designation and any quantity override.
        With `plan=None` this behaves exactly as it did before Phase 51,
        including raising DuplicateIdentityError on a colliding identity.
        """
        plan = plan or IngestionPlan()
        
        # Phase 32 (3.1): per-stage timing instrumentation so freezes on heavy
        # BOMs can be attributed to a concrete pipeline stage from the log.
        t_start = time.perf_counter()
        t_prev = t_start

        def _emit(stage: ProgressStage, detail: dict[str, Any] | None = None) -> None:
            nonlocal t_prev
            now = time.perf_counter()
            logger.info(
                "ingest stage %s reached at +%.3fs (stage took %.3fs)",
                stage.value, now - t_start, now - t_prev
            )
            t_prev = now
            if progress is not None:
                progress(ProgressEvent(stage=stage, detail=detail))

        gatekeeper.validate(paths)
        _emit(ProgressStage.GATEKEEPER_PASSED)

        quartet = categorizer.categorize(paths)
        _emit(ProgressStage.FILES_CATEGORIZED)
        
        bom_hash = hashing.sha256_hex(quartet.bom_path)
        trav_hash = hashing.sha256_hex(quartet.traveler_path)
        notes_hash = hashing.sha256_hex(quartet.notes_path)
        pdf_hash = hashing.sha256_hex(quartet.pdf_path) if quartet.pdf_path else None
        
        if pdf_hash:
            _emit(ProgressStage.PDF_HASHED)
        _emit(ProgressStage.FILES_HASHED)

        # Parse part number early from BOM name to construct storage path
        # In case of mismatch, cross_validation will catch it, but we need
        # a directory name now.
        temp_part_number = derive_part_number_from_filename(quartet.bom_path)
        
        from .locator import JOB_NUMBER_GRAMMAR
        from .errors import JobNumberMalformed
        if not JOB_NUMBER_GRAMMAR.match(temp_part_number):
            raise JobNumberMalformed(temp_part_number)
            
        audit_dir = self.file_storage_root / temp_part_number / "unsplit"

        try:
            audit_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise FileStorageError(audit_dir, audit_dir, e)

        # Phase 51 section 2.2: content-addressed, so a revision that kept its
        # filename gets a distinct destination instead of silently resolving
        # onto the stored one and registering the new hash against old bytes.
        # The hashes are already in hand from the block above.
        stored_bom = content_addressed_path(audit_dir, quartet.bom_path, bom_hash)
        stored_trav = content_addressed_path(audit_dir, quartet.traveler_path, trav_hash)
        stored_notes = content_addressed_path(audit_dir, quartet.notes_path, notes_hash)
        stored_pdf = (
            content_addressed_path(audit_dir, quartet.pdf_path, pdf_hash)
            if quartet.pdf_path else None
        )

        copied_paths = []
        src = dst = None
        try:
            copy_targets = [
                (quartet.bom_path, stored_bom, bom_hash),
                (quartet.traveler_path, stored_trav, trav_hash),
                (quartet.notes_path, stored_notes, notes_hash),
            ]
            if quartet.pdf_path and stored_pdf:
                copy_targets.append((quartet.pdf_path, stored_pdf, pdf_hash))

            for src, dst, expected_hash in copy_targets:
                if not dst.exists():
                    shutil.copy2(src, dst)
                    copied_paths.append(dst)
                # Section 2.3: both branches converge on one post-condition --
                # the file at dst hashes to expected_hash. Verifying only the
                # skip branch would leave a truncated copy unchecked, and the
                # registered hash is what the startup sweep matches on.
                if hashing.sha256_hex(dst) != expected_hash:
                    raise StoredFileVerificationError(src, dst)

            if quartet.pdf_path:
                _emit(ProgressStage.PDF_COPIED)
            _emit(ProgressStage.FILES_COPIED)
        except Exception as e:
            self._unlink_copies(copied_paths)
            if isinstance(e, (FileStorageError, StoredFileVerificationError)):
                raise
            raise FileStorageError(src, dst, e)

        # Parse from stored locations
        pdf_result = None
        try:
            bom_result = audit_bom.parse(stored_bom)
            _emit(ProgressStage.BOM_PARSED, {"header_layout": getattr(bom_result, "header_layout", "canonical")})

            eco_result = eco_build_notes.parse(stored_notes)
            _emit(ProgressStage.ECO_PARSED, {"eco_item_count": eco_result.row_count})

            trav_result = traveler.parse(stored_trav, self.coord_map)
            _emit(ProgressStage.TRAVELER_PARSED)

            intent = cross_validation.reconcile(bom_result, eco_result, trav_result, self.coord_map)
            _emit(ProgressStage.CROSS_VALIDATED)

            # Section 3.2: the operator confirmed a replacement against the
            # identity the probe read. If the authoritative parse disagrees, the
            # share changed underneath them and consent no longer covers what is
            # about to happen. Abort before the savepoint.
            self._assert_matches_probe(plan, intent, trav_hash)
            
            if stored_pdf:
                # Gather all RefDes values across all BOM items
                target_ref_des = set()
                for item in intent.bom_items:
                    if item.ref_des_list:
                        target_ref_des.update(item.ref_des_list)
                        
                pdf_result = self.layout_parser.parse(stored_pdf, target_ref_des)
                _emit(ProgressStage.PDF_PARSED)
                
                # Log missing RefDes
                missing = target_ref_des - pdf_result.found_ref_des
                if missing:
                    logger.debug(f"PDF missing {len(missing)} RefDes components: {sorted(list(missing))}")
        except Exception:
            self._unlink_copies(copied_paths)
            raise

        # Section 3.5: captured before the savepoint so a rollback cannot leave
        # a reap pointing at rows that still exist.
        superseded_files = []
        seen_paths = set()
        for doomed_id in plan.replace_family:
            for sf in self.source_file_repo.list_for_audit(doomed_id):
                # clone_to_suffix clones source_files by reference, so a split
                # family names the same file once per sibling. Asking the
                # reaper to unlink it n times is n-1 guaranteed misses.
                if sf.local_storage_path in seen_paths:
                    continue
                seen_paths.add(sf.local_storage_path)
                superseded_files.append(sf)

        self.conn.execute("SAVEPOINT ingest")
        try:
            # The delete and the insert share one transaction: split across two
            # callers there would be a window in which the family is gone and
            # nothing has replaced it.
            for doomed_id in plan.replace_family:
                self.audit_repo.hard_delete(doomed_id)

            draft = dataclasses.replace(
                intent.audit_draft, article_revision=plan.article_revision
            )
            audit = self.audit_repo.create(draft)

            bom_file = self.source_file_repo.register(SourceFileDraft(
                audit_id=audit.id, file_category=SourceFileCategory.BOM,
                original_filename=quartet.bom_path.name, local_storage_path=stored_bom, file_hash=bom_hash
            ))
            trav_file = self.source_file_repo.register(SourceFileDraft(
                audit_id=audit.id, file_category=SourceFileCategory.TRAVELER,
                original_filename=quartet.traveler_path.name, local_storage_path=stored_trav, file_hash=trav_hash
            ))
            notes_file = self.source_file_repo.register(SourceFileDraft(
                audit_id=audit.id, file_category=SourceFileCategory.NOTES,
                original_filename=quartet.notes_path.name, local_storage_path=stored_notes, file_hash=notes_hash
            ))
            
            pdf_file = None
            if stored_pdf and pdf_hash:
                pdf_file = self.source_file_repo.register(SourceFileDraft(
                    audit_id=audit.id, file_category=SourceFileCategory.PDF,
                    original_filename=quartet.pdf_path.name, local_storage_path=stored_pdf, file_hash=pdf_hash
                ))

            if intent.bom_items:
                # Insert side-table BOM components
                bom_drafts = []
                for item in intent.bom_items:
                    if item.ref_des_list:
                        for rd in item.ref_des_list:
                            bom_drafts.append(AuditBomComponentDraft(
                                source_file_id=bom_file.id, component_mpn=item.component_mpn,
                                ref_des=rd, mount_type=item.mount_type, description=item.description,
                                find_number=item.find_number
                            ))
                if bom_drafts:
                    self.bom_component_repo.bulk_insert(bom_drafts)
                    
                # Insert THT checklist items (only where mount_type == 'T')
                through_hole_drafts = [
                    ThtChecklistItemDraft(
                        audit_id=audit.id, source_file_id=bom_file.id,
                        component_mpn=item.component_mpn, description=item.description
                    ) for item in intent.bom_items if item.mount_type == 'T'
                ]
                if through_hole_drafts:
                    self.tht_repo.insert_many(through_hole_drafts)
                
            if pdf_result and pdf_file:
                pdf_drafts = [
                    PdfComponentCoordDraft(
                        source_file_id=pdf_file.id, ref_des=c.ref_des, page_index=c.page_index,
                        x1=c.x1, y1=c.y1, x2=c.x2, y2=c.y2
                    ) for c in pdf_result.coordinates
                ]
                if pdf_drafts:
                    self.pdf_coord_repo.bulk_insert(pdf_drafts)

            _emit(ProgressStage.PERSISTED, {"tht_item_count": len(through_hole_drafts) if intent.bom_items else 0})

            if plan.quantity_override is not None:
                self.apply_quantity_override(audit.id, plan.quantity_override)
                # The row in hand predates the override; the caller must not be
                # handed an audit that disagrees with the database.
                audit = self.audit_repo.find_by_id(audit.id)

            if self._runtime_calc_svc:
                self._runtime_calc_svc.persist(audit.id)

            self.conn.execute("RELEASE SAVEPOINT ingest")

            # Section 3.5: after RELEASE, following complete_and_cleanup's
            # shipped ordering. reference_count is what keeps this safe -- a
            # role whose bytes did not change is referenced by the new row and
            # is retained, not deleted.
            if superseded_files:
                from cockpit.services.storage_reaper import StorageReaper
                try:
                    report = StorageReaper(self.source_file_repo).reap(superseded_files)
                    if report.failed_paths:
                        logger.warning(
                            "Superseded file(s) could not be removed; the startup "
                            "sweep will retry: %s", report.failed_paths
                        )
                except Exception:
                    logger.exception("Reaping the replaced family failed")

            return audit
            
        except Exception as e:
            self.conn.execute("ROLLBACK TO SAVEPOINT ingest")
            self.conn.execute("RELEASE SAVEPOINT ingest")
            
            rollback_targets = [
                (stored_bom, bom_hash), 
                (stored_trav, trav_hash), 
                (stored_notes, notes_hash)
            ]
            if stored_pdf and pdf_hash:
                rollback_targets.append((stored_pdf, pdf_hash))
                
            for cp, file_hash in rollback_targets:
                if cp in copied_paths:
                    if self.source_file_repo.reference_count(file_hash) == 0:
                        try:
                            cp.unlink()
                        except Exception as unlink_e:
                            logger.warning(f"Failed to delete {cp} during rollback: {unlink_e}")
                    else:
                        logger.info(f"Skipping deletion of {cp} during rollback; referenced by sibling.")
            
            raise
            
    def add_pdf_to_audit(
        self,
        audit_id: int,
        pdf_path: pathlib.Path,
    ) -> None:
        """Add or replace the PDF attached to an existing audit."""
        from .errors import IngestionError
        
        audit = self.audit_repo.find_by_id(audit_id)
        if not audit:
            from ..persistence.errors import AuditNotFound
            raise AuditNotFound(audit_id)

        pdf_hash = hashing.sha256_hex(pdf_path)

        audit_dir = self.file_storage_root / audit.part_number / "unsplit"
        try:
            audit_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise FileStorageError(audit_dir, audit_dir, e)

        # Phase 50 section 3.2: content-addressed, so a revision re-exported
        # under the same filename gets a distinct destination instead of
        # silently keeping the superseded bytes under the new hash.
        stored_pdf = audit_dir / f"{pdf_path.stem}__{pdf_hash[:12]}{pdf_path.suffix}"
        copied = False
        try:
            if not stored_pdf.exists():
                shutil.copy2(pdf_path, stored_pdf)
                copied = True
        except Exception as e:
            raise FileStorageError(pdf_path, stored_pdf, e)

        # Phase 50 section 3.3: both branches converge on one post-condition —
        # the file at stored_pdf hashes to pdf_hash. Verifying only the skip
        # branch would leave a truncated copy unchecked, and the registered hash
        # is what the startup sweep matches on.
        if hashing.sha256_hex(stored_pdf) != pdf_hash:
            if copied and self.source_file_repo.reference_count(pdf_hash) == 0:
                try:
                    stored_pdf.unlink()
                except Exception:
                    pass
            raise StoredFileVerificationError(pdf_path, stored_pdf)

        target_ref_des = set()
        bom_sf = self.source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.BOM)
        if bom_sf:
            bom_components = self.bom_component_repo.list_for_source_file(bom_sf.id)
            for c in bom_components:
                target_ref_des.add(c.ref_des)

        try:
            pdf_result = self.layout_parser.parse(stored_pdf, target_ref_des)
        except Exception:
            if copied:
                try:
                    stored_pdf.unlink()
                except Exception:
                    pass
            raise

        # Phase 50 section 3.4: captured before the savepoint so that a rollback
        # cannot leave a reap referring to rows that still exist. The reap runs
        # after RELEASE, following complete_and_cleanup's ordering.
        superseded = []
        prior_before_txn = self.source_file_repo.find_by_audit_and_category(
            audit_id, SourceFileCategory.PDF
        )
        if prior_before_txn:
            superseded.append(prior_before_txn)

        self.conn.execute("SAVEPOINT add_pdf")
        try:
            prior_pdf_sf = self.source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.PDF)
            if prior_pdf_sf:
                self.conn.execute("DELETE FROM source_files WHERE id = ?", (prior_pdf_sf.id,))
                
            pdf_file = self.source_file_repo.register(SourceFileDraft(
                audit_id=audit.id, file_category=SourceFileCategory.PDF,
                # The operator-facing name, not the content-addressed one on
                # disk: nothing derives a path from this column and the UI
                # displays it, so the hash suffix must not reach it.
                original_filename=pdf_path.name, local_storage_path=stored_pdf, file_hash=pdf_hash
            ))
            
            if pdf_result and pdf_file:
                pdf_drafts = [
                    PdfComponentCoordDraft(
                        source_file_id=pdf_file.id, ref_des=c.ref_des, page_index=c.page_index,
                        x1=c.x1, y1=c.y1, x2=c.x2, y2=c.y2
                    ) for c in pdf_result.coordinates
                ]
                if pdf_drafts:
                    self.pdf_coord_repo.bulk_insert(pdf_drafts)

            if self._runtime_calc_svc:
                self._runtime_calc_svc.persist(audit.id)

            self.conn.execute("RELEASE SAVEPOINT add_pdf")
        except Exception:
            self.conn.execute("ROLLBACK TO SAVEPOINT add_pdf")
            self.conn.execute("RELEASE SAVEPOINT add_pdf")
            if copied:
                if self.source_file_repo.reference_count(pdf_hash) == 0:
                    try:
                        stored_pdf.unlink()
                    except Exception:
                        pass
            raise

        # reference_count is what keeps this safe for split siblings, which
        # share local_storage_path and file_hash by reference: a file another
        # row still points at is retained, not deleted. A reap failure leaves
        # the rows correct and an unreferenced file for the startup sweep.
        if superseded:
            from cockpit.services.storage_reaper import StorageReaper
            try:
                report = StorageReaper(self.source_file_repo).reap(superseded)
                if report.failed_paths:
                    logger.warning(
                        "Superseded drawing(s) could not be removed; the startup "
                        "sweep will retry: %s", report.failed_paths
                    )
            except Exception:
                logger.exception("Reaping the superseded drawing failed")

    def add_secondary_pdf_to_audit(
        self,
        audit_id: int,
        pdf_path: pathlib.Path,
    ) -> None:
        """Add or replace the secondary PDF attached to an existing audit."""
        from ..persistence.errors import AuditNotFound
        
        audit = self.audit_repo.find_by_id(audit_id)
        if not audit:
            raise AuditNotFound(audit_id)

        pdf_hash = hashing.sha256_hex(pdf_path)
        
        audit_dir = self.file_storage_root / audit.part_number / "unsplit" / "secondary"
        try:
            audit_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise FileStorageError(audit_dir, audit_dir, e)

        stored_pdf = audit_dir / f"{pdf_hash}{pdf_path.suffix}"
        copied = False
        try:
            if not stored_pdf.exists():
                shutil.copy2(pdf_path, stored_pdf)
                copied = True
        except Exception as e:
            raise FileStorageError(pdf_path, stored_pdf, e)

        self.conn.execute("SAVEPOINT add_secondary_pdf")
        try:
            prior_sf = self.source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.SECONDARY_PDF)
            if prior_sf:
                self.conn.execute("DELETE FROM source_files WHERE id = ?", (prior_sf.id,))
                
            self.source_file_repo.register(SourceFileDraft(
                audit_id=audit.id, file_category=SourceFileCategory.SECONDARY_PDF,
                original_filename=pdf_path.name, local_storage_path=stored_pdf, file_hash=pdf_hash
            ))
            self.conn.execute("RELEASE SAVEPOINT add_secondary_pdf")
        except Exception:
            self.conn.execute("ROLLBACK TO SAVEPOINT add_secondary_pdf")
            self.conn.execute("RELEASE SAVEPOINT add_secondary_pdf")
            if copied:
                if self.source_file_repo.reference_count(pdf_hash) == 0:
                    try:
                        stored_pdf.unlink()
                    except Exception:
                        pass
            raise
