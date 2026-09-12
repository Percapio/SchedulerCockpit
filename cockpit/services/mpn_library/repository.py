from cockpit.persistence.repositories.bom_components import PersistedBomLine
from typing import List, Optional, Union, Callable
from datetime import datetime
from .types import (
    MpnKey,
    AttributeName,
    Provenance,
    PartAttributeDraft,
    ResolvedAttribute,
    ObservationSummary,
    Absent,
    Unkeyable
)
from .connection import LibraryConnection
from .registry import ATTRIBUTE_REGISTRY
from .normalisation import normalise_mpn, NORMALISATION_VERSION
from cockpit.services.library_errors import UnknownAttributeName


class LibraryRepository:
    def __init__(self, conn: LibraryConnection):
        self._conn = conn

    def observe_bom(
        self,
        bom_lines: List[PersistedBomLine],
        utcnow: Callable[[], datetime]
    ) -> ObservationSummary:
        """
        Records every distinct normalised MPN on an audit BOM as a library part.
        Returns ObservationSummary for the grid header.
        """
        cur = self._conn.cursor()
        
        cur.execute("SELECT mpn_as_seen FROM library_part")
        existing_as_seen = {row["mpn_as_seen"] for row in cur.fetchall()}
        
        seen_in_bom = set()
        for line in bom_lines:
            seen_in_bom.add(line.component_mpn)
            
        new_unresolved_count = 0
        new_unkeyable_count = 0
        existing_count = 0
        
        cur.execute("BEGIN IMMEDIATE")
        try:
            now_iso = utcnow().isoformat()
            
            for raw_mpn in seen_in_bom:
                if raw_mpn in existing_as_seen:
                    existing_count += 1
                    continue
                    
                result = normalise_mpn(raw_mpn)
                
                if isinstance(result, Unkeyable):
                    # For unkeyable, we use the raw text as the key to avoid collisions,
                    # but actually we probably want a synthetic key or just the raw_mpn as the key?
                    # The doc says "keys that fail normalisation are inserted at status UNKEYABLE 
                    # carrying their raw text in mpn_as_seen." It also says "mpn_key TEXT PRIMARY KEY".
                    # Let's use `unkeyable:<raw_mpn>` as key to prevent normalisation conflicts?
                    # No, normalisation only produces printable ASCII. The raw mpn might not be.
                    # Wait, let's use the raw string if it's unkeyable, because it's distinct?
                    # If multiple unkeyable strings are identical, they get identical keys.
                    key = f"unkeyable:{hash(raw_mpn)}"
                    # Wait, if `raw_mpn` is unkeyable, we can just insert it using a hash or UUID. 
                    # Actually, if the verbatim string is the same, it should be the same row, so use raw_mpn as key?
                    # But if raw_mpn has non-ASCII, sqlite handles it fine. Let's just use raw_mpn.
                    key = raw_mpn
                    
                    cur.execute(
                        """
                        INSERT OR IGNORE INTO library_part 
                        (mpn_key, mpn_as_seen, normalisation_version, resolution_status, first_seen_at)
                        VALUES (?, ?, ?, 'UNKEYABLE', ?)
                        """,
                        (key, raw_mpn, NORMALISATION_VERSION, now_iso)
                    )
                    if cur.rowcount > 0:
                        new_unkeyable_count += 1
                        existing_as_seen.add(raw_mpn)
                    else:
                        existing_count += 1
                else:
                    mpn_key = result
                    cur.execute(
                        """
                        INSERT OR IGNORE INTO library_part 
                        (mpn_key, mpn_as_seen, normalisation_version, resolution_status, first_seen_at)
                        VALUES (?, ?, ?, 'UNRESOLVED', ?)
                        """,
                        (mpn_key, raw_mpn, NORMALISATION_VERSION, now_iso)
                    )
                    # Note: due to normalization, multiple raw MPNs could map to same mpn_key.
                    # The IGNORE handles duplicates. 
                    # But mpn_as_seen tracking: we should only count as new if it actually inserted.
                    if cur.rowcount > 0:
                        new_unresolved_count += 1
                        existing_as_seen.add(raw_mpn)
                    else:
                        existing_count += 1

            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
            
        return ObservationSummary(
            new_unresolved_count=new_unresolved_count,
            new_unkeyable_count=new_unkeyable_count,
            existing_count=existing_count
        )

    def record_override(
        self,
        draft: PartAttributeDraft,
        utcnow: Callable[[], datetime]
    ) -> None:
        if draft.attribute_name not in ATTRIBUTE_REGISTRY:
            raise UnknownAttributeName(draft.attribute_name)
            
        cur = self._conn.cursor()
        now_iso = utcnow().isoformat()
        
        cur.execute("BEGIN IMMEDIATE")
        try:
            # Check if exists
            cur.execute(
                """
                SELECT value_text, value_numeric, recorded_at, confirmed_at 
                FROM part_attribute 
                WHERE mpn_key = ? AND attribute_name = ? AND provenance = ?
                """,
                (draft.mpn_key, draft.attribute_name, draft.provenance.value)
            )
            row = cur.fetchone()
            
            if row:
                # Update in place
                if row["value_text"] != draft.value_text or row["value_numeric"] != draft.value_numeric:
                    confirmed_at = None
                else:
                    confirmed_at = row["confirmed_at"].isoformat() if hasattr(row["confirmed_at"], "isoformat") else row["confirmed_at"]
                    
                cur.execute(
                    """
                    UPDATE part_attribute
                    SET value_text = ?, value_numeric = ?, confirmed_at = ?
                    WHERE mpn_key = ? AND attribute_name = ? AND provenance = ?
                    """,
                    (
                        draft.value_text,
                        draft.value_numeric,
                        confirmed_at,
                        draft.mpn_key,
                        draft.attribute_name,
                        draft.provenance.value
                    )
                )
            else:
                # Insert
                cur.execute(
                    """
                    INSERT INTO part_attribute
                    (mpn_key, attribute_name, provenance, value_text, value_numeric, source_label, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft.mpn_key,
                        draft.attribute_name,
                        draft.provenance.value,
                        draft.value_text,
                        draft.value_numeric,
                        draft.source_label,
                        now_iso
                    )
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def confirm_attribute(
        self,
        mpn_key: MpnKey,
        attribute_name: AttributeName,
        utcnow: Callable[[], datetime]
    ) -> None:
        if attribute_name not in ATTRIBUTE_REGISTRY:
            raise UnknownAttributeName(attribute_name)
            
        cur = self._conn.cursor()
        now_iso = utcnow().isoformat()
        
        cur.execute("BEGIN IMMEDIATE")
        try:
            # Must find the highest-precedence value to confirm
            resolved = self._resolve_attribute_internal(cur, mpn_key, attribute_name)
            if not isinstance(resolved, ResolvedAttribute):
                self._conn.commit()
                return
                
            cur.execute(
                """
                UPDATE part_attribute
                SET confirmed_at = ?
                WHERE mpn_key = ? AND attribute_name = ? AND provenance = ?
                """,
                (now_iso, mpn_key, attribute_name, resolved.provenance.value)
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def revoke_override(
        self,
        mpn_key: MpnKey,
        attribute_name: AttributeName
    ) -> None:
        if attribute_name not in ATTRIBUTE_REGISTRY:
            raise UnknownAttributeName(attribute_name)
            
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute(
                """
                DELETE FROM part_attribute
                WHERE mpn_key = ? AND attribute_name = ? AND provenance = ?
                """,
                (mpn_key, attribute_name, Provenance.OPERATOR_OVERRIDE.value)
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def resolve_attribute(
        self,
        mpn_key: MpnKey,
        attribute_name: AttributeName
    ) -> Union[ResolvedAttribute, Absent]:
        if attribute_name not in ATTRIBUTE_REGISTRY:
            raise UnknownAttributeName(attribute_name)
            
        cur = self._conn.cursor()
        return self._resolve_attribute_internal(cur, mpn_key, attribute_name)

    def _resolve_attribute_internal(self, cur, mpn_key: MpnKey, attribute_name: AttributeName) -> Union[ResolvedAttribute, Absent]:
        cur.execute(
            """
            SELECT provenance, value_text, value_numeric, source_label, recorded_at, confirmed_at
            FROM part_attribute
            WHERE mpn_key = ? AND attribute_name = ?
            """,
            (mpn_key, attribute_name)
        )
        rows = cur.fetchall()
        if not rows:
            return Absent()
            
        # Map rows by provenance string
        row_map = {row["provenance"]: row for row in rows}
        
        for prov in [Provenance.OPERATOR_OVERRIDE, Provenance.REFERENCE_SHEET, Provenance.DIGIKEY]:
            if prov.value in row_map:
                r = row_map[prov.value]
                # convert datetime objects back to ISO string for the typed return if needed,
                # but hydrating_row_factory already turned _at columns into datetimes.
                # The ResolvedAttribute type expects strings for datetimes?
                # The doc says "provenance, its recorded_at, and whether a human has confirmed it"
                rec_at = r["recorded_at"].isoformat() if hasattr(r["recorded_at"], "isoformat") else r["recorded_at"]
                conf_at = r["confirmed_at"]
                if conf_at and hasattr(conf_at, "isoformat"):
                    conf_at = conf_at.isoformat()
                    
                return ResolvedAttribute(
                    mpn_key=mpn_key,
                    attribute_name=attribute_name,
                    provenance=prov,
                    value_text=r["value_text"],
                    value_numeric=r["value_numeric"],
                    source_label=r["source_label"],
                    recorded_at=rec_at,
                    confirmed_at=conf_at
                )
                
        return Absent()
