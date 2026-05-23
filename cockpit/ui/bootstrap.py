"""App bootstrapping module."""

import logging
from logging.handlers import RotatingFileHandler
import sqlite3
import sys
from dataclasses import dataclass

from cockpit.persistence.connection import open_connection
from cockpit.persistence.schema import migrate_to_v1
from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.repositories.tht_checklist import ThtChecklistRepository
from cockpit.persistence.repositories.notes_checklist import BuildNotesChecklistRepository
from cockpit.ingestion.service import IngestionService
from cockpit.ingestion.parsers.coordinate_map import load as load_map
from cockpit.services.audit_read import AuditReadService
from cockpit.services.checklist import ChecklistService
from cockpit.services.split import AuditSplitService

from .config import AppConfig


@dataclass(frozen=True)
class BootstrappedApp:
    config: AppConfig
    conn: sqlite3.Connection
    ingestion_service: IngestionService
    audit_read_svc: AuditReadService
    checklist_svc: ChecklistService
    split_svc: AuditSplitService


def bootstrap(config: AppConfig) -> BootstrappedApp:
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

    logging.getLogger("cockpit").info("Bootstrapping Cockpit application...")
    
    conn = open_connection(config.db_path)
    migrate_to_v1(conn)
    
    audit_repo = AuditRepository(conn)
    source_file_repo = SourceFileRepository(conn)
    tht_repo = ThtChecklistRepository(conn)
    notes_repo = BuildNotesChecklistRepository(conn)
    
    coord_map = load_map(config.coord_map_path)
    
    ingestion_service = IngestionService(
        conn=conn,
        audit_repo=audit_repo,
        source_file_repo=source_file_repo,
        tht_repo=tht_repo,
        notes_repo=notes_repo,
        coord_map=coord_map,
        file_storage_root=config.file_storage_root
    )
    
    audit_read_svc = AuditReadService(audit_repo)
    checklist_svc = ChecklistService(audit_repo, tht_repo, notes_repo)
    split_svc = AuditSplitService(conn, audit_repo)

    return BootstrappedApp(
        config=config,
        conn=conn,
        ingestion_service=ingestion_service,
        audit_read_svc=audit_read_svc,
        checklist_svc=checklist_svc,
        split_svc=split_svc
    )
