"""PDF component coordinate repository."""

import sqlite3
from typing import Sequence

from ..errors import DuplicatePdfCoordError, InvalidArgumentError, PersistenceUnavailable
from ..types import PdfComponentCoord, PdfComponentCoordDraft


class PdfComponentCoordRepository:

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def bulk_insert(self, drafts: Sequence[PdfComponentCoordDraft]) -> int:
        if not drafts:
            raise InvalidArgumentError("drafts", drafts, "Cannot insert empty drafts list")

        try:
            cur = self._conn.cursor()
            cur.executemany(
                """
                INSERT INTO pdf_component_coords
                (source_file_id, ref_des, page_index, x1, y1, x2, y2)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [(d.source_file_id, d.ref_des, d.page_index, d.x1, d.y1, d.x2, d.y2)
                 for d in drafts]
            )
            return cur.rowcount
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed" in str(e):
                raise DuplicatePdfCoordError(drafts[0].source_file_id, "unknown (bulk insert)", drafts[0].page_index) from e
            raise PersistenceUnavailable(self._conn, e) from e
        except sqlite3.Error as e:
            raise PersistenceUnavailable(self._conn, e) from e

    def list_for_source_file(self, source_file_id: int) -> list[PdfComponentCoord]:
        try:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT id, source_file_id, ref_des, page_index, x1, y1, x2, y2
                FROM pdf_component_coords
                WHERE source_file_id = ?
                ORDER BY page_index ASC, ref_des ASC
                """,
                (source_file_id,)
            )
            return [
                PdfComponentCoord(
                    id=row["id"],
                    source_file_id=row["source_file_id"],
                    ref_des=row["ref_des"],
                    page_index=row["page_index"],
                    x1=row["x1"],
                    y1=row["y1"],
                    x2=row["x2"],
                    y2=row["y2"]
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
                INSERT INTO pdf_component_coords
                (source_file_id, ref_des, page_index, x1, y1, x2, y2)
                SELECT ?, ref_des, page_index, x1, y1, x2, y2
                FROM pdf_component_coords
                WHERE source_file_id = ?
                """,
                (dst_source_file_id, src_source_file_id)
            )
            return cur.rowcount
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed" in str(e):
                raise DuplicatePdfCoordError(dst_source_file_id, "unknown (clone)", -1) from e
            raise PersistenceUnavailable(self._conn, e) from e
        except sqlite3.Error as e:
            raise PersistenceUnavailable(self._conn, e) from e
