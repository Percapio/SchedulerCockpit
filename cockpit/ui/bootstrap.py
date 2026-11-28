"""App bootstrapping module."""

import logging
import os
import shutil
import sqlite3
import sys
import pathlib
from dataclasses import dataclass
from typing import Any, Callable
from cockpit.services.runtime_constants import RuntimeConstants
from cockpit.services.runtime_calc import RuntimeCalcService

from cockpit.persistence.connection import open_connection
from cockpit.persistence.schema import migrate
from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.repositories.tht_checklist import ThtChecklistRepository
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from cockpit.ingestion.service import IngestionService
from cockpit.ingestion.parsers.coordinate_map import load as load_map
from cockpit.services.audit_read import AuditReadService
from cockpit.services.checklist import ChecklistService
from cockpit.services.split import AuditSplitService

from cockpit.services.storage_reaper import StorageReaper
from cockpit.services.completion import CompletionService
from cockpit.services.startup_reconciler import StartupReconciler
from cockpit.services.layout_query import LayoutQueryService
from cockpit.services.release import ReleaseService
from cockpit.services.setup_bom import SetupBomService
from cockpit.services.views import ReconciliationReport
from cockpit.layout.renderer import PdfRenderer
from cockpit.ingestion.hashing import sha256_hex

from cockpit.ui.runtime import install_dir

from .config import AppConfig


@dataclass(frozen=True)
class BootstrappedApp:
    config: AppConfig
    conn: sqlite3.Connection
    ingestion_service: IngestionService
    audit_repo: AuditRepository
    audit_read_svc: AuditReadService
    checklist_svc: ChecklistService
    split_svc: AuditSplitService
    completion_svc: CompletionService
    layout_query_svc: LayoutQueryService
    release_svc: ReleaseService
    setup_bom_svc: SetupBomService
    holiday_svc: Any  # Use Any to avoid circular imports or just import it at top
    pdf_renderer: PdfRenderer
    reconciliation_report: ReconciliationReport
    runtime_calc_svc: RuntimeCalcService
    source_file_repo: SourceFileRepository
    audit_bom_component_repo: AuditBomComponentRepository
    storage_reaper: StorageReaper
    library_module: Any | None
    # Phase 57: the drawing worker's inputs and parsers, and the placements read back.
    parser_registry: Any = None
    update_repositories: Any = None
    placement_query_svc: Any = None


def remove_notes_media_tree(app_data_root: pathlib.Path) -> bool:
    """Deletes the notes_media/ tree left behind by the pre-v16 image cache.

    pre:  schema v16 has applied, so nothing reads from the tree any more
    post: returns True when the tree is gone. Best-effort: a locked file leaves
          a partial tree, which is inert, and the next launch retries. This must
          never be able to block startup.
    """
    tree = pathlib.Path(app_data_root) / "notes_media"
    try:
        if not tree.exists():
            return True
        shutil.rmtree(tree)
        logger.info("Removed obsolete notes media cache at %s", tree)
        return True
    except OSError as exc:
        logger.warning("Could not remove obsolete notes media cache at %s: %s", tree, exc)
        return False


def bootstrap(
    config: AppConfig,
    constants_provider: Callable[[], RuntimeConstants] = RuntimeConstants.defaults
) -> BootstrappedApp:
    """Wire the full object graph for one application session."""
    
    # Configure logging
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG if config.log_level == "DEBUG" else logging.INFO)
    
    # Remove existing handlers to avoid duplicates in testing
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        
    formatter = logging.Formatter('%(asctime)s - %(threadName)s - %(name)s - %(levelname)s - %(message)s')

    from cockpit.ui.runtime import runtime_kind
    from cockpit._build_info import get_build_info
    from cockpit.ui.logging_faults import CountingLogHandler, install_primary_handler
    build_info = get_build_info()

    # Patch 15 §6 (F24): weekly files kept by age (§8), every failed record counted, and the file opened on the
    # first record (delay=True inside the handler), so an unopenable log can't stop Cockpit starting.
    file_handler = CountingLogHandler(config.log_path, config.log_retention_days)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    install_primary_handler(file_handler)
    # Patch 15 §9: the census opens a session at this line, before anything else the session logs.
    logging.getLogger("cockpit").info("Session start: pid=%d version=%s", os.getpid(), build_info.version)

    # Patch 15 §6 (D6): a windowed build has no stderr; a handler on None failed on every WARNING.
    if sys.stderr is not None:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        stderr_handler.setLevel(logging.WARNING)
        logger.addHandler(stderr_handler)

    duplicate_dir = None
    if config.log_duplicate_to_install_dir:
        install_log_path = install_dir() / "log.txt"
        try:
            # The handler opens its file on the first record (delay=True), so the folder's writability is checked
            # here, where the old handler's eager open used to fail and warn. Its later faults aren't surfaced: it is
            # an optional copy (Patch 15 §6).
            with open(install_log_path, "a", encoding="utf-8"):
                pass
            install_handler = CountingLogHandler(install_log_path, config.log_retention_days)
            install_handler.setFormatter(formatter)
            logger.addHandler(install_handler)
            duplicate_dir = install_log_path.parent
            logger.info("Duplicate log destination: %s", install_log_path)
        except OSError as e:
            # Install dir may be read-only (e.g. Program Files without elevation)
            logger.warning("Could not open install-folder log at %s: %s — primary log at %s", install_log_path, e, config.log_path)

    logging.getLogger("cockpit").info("Primary log: %s", config.log_path)
    logging.getLogger("cockpit").info("Build info: version=%s commit=%s runtime=%s",
                                       build_info.version, build_info.commit, runtime_kind().value)

    # Patch 15 §8 (F26): logs and crash reports past the retention window, in every place they are written.
    import time as _time
    from cockpit.ui.retention import prune_by_age, retention_targets
    prune_by_age(
        retention_targets(
            log_dir=config.log_path.parent,
            crash_dir=config.crash_dir,
            data_root=config.app_data_root.parent,
            duplicate_dir=duplicate_dir,
        ),
        config.log_retention_days,
        _time.time(),
    )

    # Format the probe history
    probe_summary = []
    for p in config.probe_history:
        if p.success:
            probe_summary.append(f"{p.candidate_label} -> OK")
        else:
            probe_summary.append(f"{p.candidate_label} -> {p.error}")
    
    logging.getLogger("cockpit").info("Application data root: %s (Probe history: %s)", config.app_data_root.parent, ", ".join(probe_summary))
    logging.getLogger("cockpit").info("Bootstrapping Cockpit application...")
    
    conn = open_connection(
        config.db_path,
        cache_size_kib=config.sqlite_cache_kib,
        mmap_size_bytes=config.sqlite_mmap_bytes,
        wal_autocheckpoint_pages=config.wal_autocheckpoint_pages
    )
    
    bom_component_repo = AuditBomComponentRepository(conn)
    pdf_coord_repo = PdfComponentCoordRepository(conn)
    
    from cockpit.layout.parser import PdfLayoutParser
    from cockpit.protocols import ParserRegistry
    from cockpit.ingestion.parsers import audit_bom, eco_build_notes, traveler
    
    class _BomParserWrapper:
        def parse(self, path):
            return audit_bom.parse(path)
            
    coord_map = load_map(config.coord_map_path)
            
    pdf_renderer = PdfRenderer()
            
    parser_registry = ParserRegistry(
        bom_parser=_BomParserWrapper(),
        eco_parser=eco_build_notes,
        traveler_parser=traveler,
        pdf_layout_parser=PdfLayoutParser(),
        coord_map=coord_map
    )
    
    needs_backfill = migrate(conn, parser_registry)
    remove_notes_media_tree(config.app_data_root)

    audit_repo = AuditRepository(conn, bom_component_repo, pdf_coord_repo)
    source_file_repo = SourceFileRepository(conn)
    tht_repo = ThtChecklistRepository(conn)

    from cockpit.persistence.repositories.placements import (
        BoardPlacementRepository, PageRegistrationRepository, PlacementRejectionRepository,
    )
    from cockpit.services.placements import PlacementQueryService
    board_placement_repo = BoardPlacementRepository(conn)
    page_registration_repo = PageRegistrationRepository(conn)
    placement_rejection_repo = PlacementRejectionRepository(conn)
    placement_query_svc = PlacementQueryService(
        source_file_repo, bom_component_repo, pdf_coord_repo,
        board_placement_repo, page_registration_repo, placement_rejection_repo,
    )

    from cockpit.services.runtime_calc import RuntimeCalcService
    runtime_calc_svc = RuntimeCalcService(
        audit_repo, source_file_repo, bom_component_repo, pdf_coord_repo,
        constants_provider=constants_provider,
        placement_query=placement_query_svc,
    )
    
    if needs_backfill:
        logger.info("Starting runtime backfill")
        # one-time backfill
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM active_audits")
            for r in cur.fetchall():
                audit_id = r["id"]
                try:
                    runtime_calc_svc.persist(audit_id)
                except Exception as e:
                    logger.error(f"Failed to backfill runtimes for audit {audit_id}: {e}")
        except Exception as e:
            logger.error(f"Error during backfill: {e}")
    
    layout_query_svc = LayoutQueryService(
        source_file_repo=source_file_repo,
        pdf_renderer=pdf_renderer,
        bom_component_repo=bom_component_repo,
        pdf_coord_repo=pdf_coord_repo,
        placement_query=placement_query_svc,
    )
    
    coord_map = load_map(config.coord_map_path)
    
    ingestion_service = IngestionService(
        conn=conn,
        audit_repo=audit_repo,
        source_file_repo=source_file_repo,
        tht_repo=tht_repo,
        bom_component_repo=bom_component_repo,
        pdf_coord_repo=pdf_coord_repo,
        layout_parser=parser_registry.pdf_layout_parser,
        coord_map=coord_map,
        file_storage_root=config.file_storage_root,
        runtime_calc_svc=runtime_calc_svc
    )
    
    from cockpit.services.holiday import HolidayRepository, HolidayService
    holiday_repo = HolidayRepository(conn)
    holiday_svc = HolidayService(holiday_repo)
    
    audit_read_svc = AuditReadService(audit_repo, holiday_svc=holiday_svc)
    checklist_svc = ChecklistService(conn, audit_repo, tht_repo, source_file_repo, bom_component_repo, config)
    split_svc = AuditSplitService(conn, audit_repo, runtime_calc_svc=runtime_calc_svc)
    
    storage_reaper = StorageReaper(source_file_repo)
    completion_svc = CompletionService(conn, audit_repo, source_file_repo, storage_reaper)
    
    release_svc = ReleaseService(audit_repo)
    setup_bom_svc = SetupBomService(
        audit_repo, bom_component_repo, pdf_coord_repo, source_file_repo, placement_query=placement_query_svc,
    )

    from cockpit.services.document_updates import FamilyUpdateRepositories
    update_repositories = FamilyUpdateRepositories(
        conn=conn, audits=audit_repo, source_files=source_file_repo, bom_components=bom_component_repo,
        tht_checklist=tht_repo, pdf_coords=pdf_coord_repo, board_placements=board_placement_repo,
        page_registrations=page_registration_repo, placement_rejections=placement_rejection_repo,
    )
    
    startup_reconciler = StartupReconciler(
        audit_repo=audit_repo,
        source_file_repo=source_file_repo,
        completion_service=completion_svc,
        file_storage_root=config.file_storage_root,
        hash_for_path=sha256_hex
    )
    report = startup_reconciler.reconcile()
    
    if report.errors or report.orphan_delete_failed:
        logger.warning(f"Reconciliation encountered issues: errors={len(report.errors)}, orphan_delete_failed={len(report.orphan_delete_failed)}")
    else:
        logger.info("Reconciliation completed successfully.")

    return BootstrappedApp(
        config=config,
        conn=conn,
        ingestion_service=ingestion_service,
        audit_repo=audit_repo,
        audit_read_svc=audit_read_svc,
        checklist_svc=checklist_svc,
        split_svc=split_svc,
        completion_svc=completion_svc,
        layout_query_svc=layout_query_svc,
        release_svc=release_svc,
        setup_bom_svc=setup_bom_svc,
        holiday_svc=holiday_svc,
        pdf_renderer=pdf_renderer,
        reconciliation_report=report,
        runtime_calc_svc=runtime_calc_svc,
        source_file_repo=source_file_repo,
        audit_bom_component_repo=bom_component_repo,
        storage_reaper=storage_reaper,
        library_module=None,
        parser_registry=parser_registry,
        update_repositories=update_repositories,
        placement_query_svc=placement_query_svc,
    )
