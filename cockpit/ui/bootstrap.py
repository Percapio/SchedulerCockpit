"""App bootstrapping module."""

import logging
from logging.handlers import RotatingFileHandler
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
from cockpit.persistence.repositories.notes_checklist import BuildNotesChecklistRepository
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
    
    file_handler = RotatingFileHandler(
        config.log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.setLevel(logging.WARNING)
    logger.addHandler(stderr_handler)

    if config.log_duplicate_to_install_dir:
        install_log_path = install_dir() / "log.txt"
        try:
            install_handler = RotatingFileHandler(
                install_log_path, maxBytes=10 * 1024 * 1024, backupCount=2, encoding="utf-8"
            )
            install_handler.setFormatter(formatter)
            logger.addHandler(install_handler)
            logger.info("Duplicate log destination: %s", install_log_path)
        except OSError as e:
            # Install dir may be read-only (e.g. Program Files without elevation)
            logger.warning("Could not open install-folder log at %s: %s — primary log at %s", install_log_path, e, config.log_path)

    from cockpit.ui.runtime import runtime_kind
    from cockpit._build_info import get_build_info
    build_info = get_build_info()
    
    logging.getLogger("cockpit").info("Primary log: %s", config.log_path)
    logging.getLogger("cockpit").info("Build info: version=%s commit=%s runtime=%s",
                                       build_info.version, build_info.commit, runtime_kind().value)

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
    
    audit_repo = AuditRepository(conn, bom_component_repo, pdf_coord_repo)
    source_file_repo = SourceFileRepository(conn)
    tht_repo = ThtChecklistRepository(conn)
    notes_repo = BuildNotesChecklistRepository(conn)
    
    from cockpit.services.runtime_calc import RuntimeCalcService
    runtime_calc_svc = RuntimeCalcService(
        audit_repo, source_file_repo, bom_component_repo, pdf_coord_repo,
        constants_provider=constants_provider
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
        pdf_coord_repo=pdf_coord_repo
    )
    
    coord_map = load_map(config.coord_map_path)
    
    ingestion_service = IngestionService(
        conn=conn,
        audit_repo=audit_repo,
        source_file_repo=source_file_repo,
        tht_repo=tht_repo,
        notes_repo=notes_repo,
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
    checklist_svc = ChecklistService(conn, audit_repo, tht_repo, notes_repo, source_file_repo, bom_component_repo)
    split_svc = AuditSplitService(conn, audit_repo, runtime_calc_svc=runtime_calc_svc)
    
    storage_reaper = StorageReaper(source_file_repo)
    completion_svc = CompletionService(conn, audit_repo, source_file_repo, storage_reaper)
    
    release_svc = ReleaseService(audit_repo)
    setup_bom_svc = SetupBomService(audit_repo, bom_component_repo, pdf_coord_repo, source_file_repo)
    
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
        runtime_calc_svc=runtime_calc_svc
    )
