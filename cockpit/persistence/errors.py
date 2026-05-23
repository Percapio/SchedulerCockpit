"""Persistence error hierarchy."""

import pathlib
import sqlite3
from typing import Any, Literal

from .types import AuditStatus


class PersistenceError(Exception):
    """Base class for every exception raised by cockpit.persistence."""


# ---------- infrastructure ----------

class PersistenceUnavailable(PersistenceError):
    """Cannot open DB or set required PRAGMAs. Raised by open_connection."""
    def __init__(self, db_path: pathlib.Path, cause: Exception):
        super().__init__(f"Cannot initialize persistence at {db_path}: {cause}")
        self.db_path = db_path
        self.cause = cause


class SchemaInitializationError(PersistenceError):
    """A DDL statement in migrate_to_v1 failed; the init transaction was rolled back."""
    def __init__(self, statement: str, cause: sqlite3.Error):
        super().__init__(f"Schema initialization failed on statement: {statement}. Cause: {cause}")
        self.statement = statement
        self.cause = cause


class SchemaMismatch(PersistenceError):
    """The DB has a schema version this code does not understand."""
    def __init__(self, found_version: int, expected_version: int):
        super().__init__(f"Schema version mismatch: found {found_version}, expected {expected_version}")
        self.found_version = found_version
        self.expected_version = expected_version


# ---------- input validation ----------

class InvalidArgumentError(PersistenceError, ValueError):
    """Caller passed a value that the contract forbids."""
    def __init__(self, field: str, value: Any, reason: str):
        super().__init__(f"Invalid argument '{field}' (value: {value!r}): {reason}")
        self.field = field
        self.value = value
        self.reason = reason


# ---------- not-found family ----------

class AuditNotFound(PersistenceError, LookupError):
    def __init__(self, audit_id: int):
        super().__init__(f"Audit id={audit_id} not found.")
        self.audit_id = audit_id


class SourceFileNotFound(PersistenceError, LookupError):
    def __init__(self, source_file_id: int):
        super().__init__(f"SourceFile id={source_file_id} not found.")
        self.source_file_id = source_file_id


class ChecklistItemNotFound(PersistenceError, LookupError):
    """Raised by either checklist repository's set_verification when item_id is unknown."""
    def __init__(self, item_id: int, table: Literal["tht", "notes"]):
        super().__init__(f"Checklist item id={item_id} not found in {table}_checklist.")
        self.item_id = item_id
        self.table = table


# ---------- invariant violations ----------

class DuplicateIdentityError(PersistenceError):
    """UNIQUE(part_number, work_order_ref, split_suffix) violated on create/clone."""
    def __init__(self, part_number: str, work_order_ref: str, split_suffix: str):
        super().__init__(
            f"Duplicate identity: part_number='{part_number}', "
            f"work_order_ref='{work_order_ref}', split_suffix='{split_suffix}'"
        )
        self.part_number = part_number
        self.work_order_ref = work_order_ref
        self.split_suffix = split_suffix


class IllegalStateTransition(PersistenceError):
    """transition_status target is not reachable from the current status."""
    def __init__(self, audit_id: int, from_status: AuditStatus, to_status: AuditStatus):
        super().__init__(f"Illegal transition for audit_id={audit_id}: {from_status} -> {to_status}")
        self.audit_id = audit_id
        self.from_status = from_status
        self.to_status = to_status


class IncompleteChecklistError(PersistenceError):
    """transition_status(COMPLETED) attempted while at least one checklist row is unverified."""
    def __init__(self, audit_id: int, tht_unverified: int, notes_unverified: int):
        super().__init__(
            f"Audit id={audit_id} cannot be completed. "
            f"Unverified items: {tht_unverified} THT, {notes_unverified} Notes."
        )
        self.audit_id = audit_id
        self.tht_unverified = tht_unverified
        self.notes_unverified = notes_unverified


class ForeignKeyMismatch(PersistenceError):
    """A draft references a source_file_id whose audit_id differs from the draft's audit_id."""
    def __init__(self, audit_id: int, source_file_id: int, source_file_audit_id: int):
        super().__init__(
            f"FK mismatch: draft audit_id={audit_id} but source_file_id={source_file_id} "
            f"belongs to audit_id={source_file_audit_id}."
        )
        self.audit_id = audit_id
        self.source_file_id = source_file_id
        self.source_file_audit_id = source_file_audit_id


class DuplicateRefDesError(PersistenceError):
    """UNIQUE(source_file_id, ref_des) violated on audit_bom_components."""
    def __init__(self, source_file_id: int, ref_des: str):
        super().__init__(f"Duplicate RefDes '{ref_des}' for source_file_id={source_file_id}")
        self.source_file_id = source_file_id
        self.ref_des = ref_des


class DuplicatePdfCoordError(PersistenceError):
    """UNIQUE(source_file_id, ref_des, page_index) violated on pdf_component_coords."""
    def __init__(self, source_file_id: int, ref_des: str, page_index: int):
        super().__init__(
            f"Duplicate PDF coord for RefDes '{ref_des}' on page {page_index} "
            f"(source_file_id={source_file_id})"
        )
        self.source_file_id = source_file_id
        self.ref_des = ref_des
        self.page_index = page_index
