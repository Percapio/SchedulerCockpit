"""Audit BOM components repository."""

import sqlite3
from typing import Sequence

from ..errors import DuplicateRefDesError, InvalidArgumentError, PersistenceUnavailable
from ..types import AuditBomComponent, AuditBomComponentDraft


class AuditBomComponentRepository:

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def bulk_insert(self, drafts: Sequence[AuditBomComponentDraft]) -> int:
        if not drafts:
            raise InvalidArgumentError("drafts", drafts, "Cannot insert empty drafts list")

        try:
            cur = self._conn.cursor()
            cur.executemany(
                """
                INSERT INTO audit_bom_components
                (source_file_id, component_mpn, ref_des, mount_type, description)
                VALUES (?, ?, ?, ?, ?)
                """,
                [(d.source_file_id, d.component_mpn, d.ref_des, d.mount_type, d.description)
                 for d in drafts]
            )
            return cur.rowcount
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed" in str(e):
                # We do not easily know which ref_des failed from the sqlite3 exception
                # unless we parse it. Since we guarantee it's uniqueness on (source_file_id, ref_des),
                # we just raise the error with the first draft's info, or we can try to extract.
                # Actually, raising with a generic message or finding the exact one might be hard.
                # Let's just raise DuplicateRefDesError with the first draft's details, as they
                # share the same source_file_id and the duplicate is likely in the set.
                raise DuplicateRefDesError(drafts[0].source_file_id, "unknown (bulk insert)") from e
            raise PersistenceUnavailable(self._conn, e) from e
        except sqlite3.Error as e:
            raise PersistenceUnavailable(self._conn, e) from e

    def list_for_source_file(self, source_file_id: int) -> list[AuditBomComponent]:
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT id, source_file_id, component_mpn, ref_des, mount_type, description
                FROM audit_bom_components
                WHERE source_file_id = ?
                ORDER BY component_mpn ASC, ref_des ASC
                """,
                (source_file_id,)
            )
            return [
                AuditBomComponent(
                    id=row["id"],
                    source_file_id=row["source_file_id"],
                    component_mpn=row["component_mpn"],
                    ref_des=row["ref_des"],
                    mount_type=row["mount_type"],
                    description=row["description"]
                )
                for row in cur.fetchall()
            ]
        except sqlite3.Error as e:
            raise PersistenceUnavailable(self._conn, e) from e

    def clone_for_source_file(
        self,
        src_source_file_id: int,
        dst_source_file_id: int,
    ) -> int:
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO audit_bom_components
                (source_file_id, component_mpn, ref_des, mount_type, description)
                SELECT ?, component_mpn, ref_des, mount_type, description
                FROM audit_bom_components
                WHERE source_file_id = ?
                """,
                (dst_source_file_id, src_source_file_id)
            )
            return cur.rowcount
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed" in str(e):
                raise DuplicateRefDesError(dst_source_file_id, "unknown (clone)") from e
            raise PersistenceUnavailable(self._conn, e) from e
        except sqlite3.Error as e:
            raise PersistenceUnavailable(self._conn, e) from e
