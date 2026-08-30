"""Image Cache Service for Build Notes Media."""

import os
import shutil
import pathlib
import sqlite3
import uuid
import logging
import docx
from dataclasses import dataclass
from typing import Generic, TypeVar

from ..persistence.clock import utcnow
from ..persistence.types import SourceFile
from ..ingestion.parsers.results import EcoImageRef
from ..ui.config import AppConfig

logger = logging.getLogger(__name__)

T = TypeVar('T')
E = TypeVar('E')

@dataclass(frozen=True)
class Result(Generic[T, E]):
    value: T | None = None
    error: E | None = None

    @property
    def is_ready(self) -> bool:
        return self.error is None

@dataclass(frozen=True)
class CachedImagePath:
    path: pathlib.Path
    notes_file_hash: str

@dataclass(frozen=True)
class ExtractionSummary:
    image_count: int
    byte_size: int
    rejected_count: int

class ImageUnavailable:
    pass

class UnsupportedFormat(ImageUnavailable): pass
class OversizeImage(ImageUnavailable): pass
class BudgetExceeded(ImageUnavailable): pass
class DocumentMissing(ImageUnavailable): pass
class ExtractionFailed(ImageUnavailable): pass

_WHITELISTED_CONTENT_TYPES = {
    'image/png': '.png',
    'image/jpeg': '.jpg',
    'image/gif': '.gif',
    'image/bmp': '.bmp',
    'image/tiff': '.tif',
}

class ImageCacheService:
    def __init__(self, conn: sqlite3.Connection, config: AppConfig):
        self.conn = conn
        self.config = config
        self.media_root = config.notes_media_root

    def _evict(self, clock_now: str, protect_hash: str) -> None:
        """Evict documents to stay within max_documents and max_total_bytes."""
        cur = self.conn.cursor()
        
        while True:
            cur.execute("SELECT COUNT(*) as c, COALESCE(SUM(byte_size), 0) as s FROM notes_media_cache")
            row = cur.fetchone()
            count = row["c"]
            total_bytes = row["s"]
            
            if int(count) <= int(self.config.max_documents) and int(total_bytes) <= int(self.config.max_total_bytes):
                break
                
            cur.execute(
                "SELECT notes_file_hash FROM notes_media_cache WHERE notes_file_hash != ? ORDER BY last_used_at ASC LIMIT 1",
                (protect_hash,)
            )
            row = cur.fetchone()
            if not row:
                break # Cannot evict anything else (only the protected hash is left, or DB is empty)
                
            victim_hash = row["notes_file_hash"]
            victim_dir = self.media_root / victim_hash
            try:
                shutil.rmtree(victim_dir)
            except Exception as e:
                logger.warning("Failed to evict directory %s: %s", victim_dir, e)
                
            cur.execute("DELETE FROM notes_media_cache WHERE notes_file_hash = ?", (victim_hash,))
            self.conn.commit()

    def ensure_extracted(
        self,
        notes_file_hash: str,
        docx_path: pathlib.Path,
    ) -> Result[ExtractionSummary, ImageUnavailable]:
        if not docx_path.exists():
            return Result(error=DocumentMissing())

        cur = self.conn.cursor()
        now_iso = utcnow().isoformat()
        
        cur.execute("SELECT notes_file_hash FROM notes_media_cache WHERE notes_file_hash = ?", (notes_file_hash,))
        if cur.fetchone():
            target_dir = self.media_root / notes_file_hash
            if target_dir.exists():
                cur.execute("UPDATE notes_media_cache SET last_used_at = ? WHERE notes_file_hash = ?", (now_iso, notes_file_hash))
                self.conn.commit()
                return Result(value=ExtractionSummary(image_count=0, byte_size=0, rejected_count=0))
            else:
                # DB has row but directory is gone, clean it up and extract again
                cur.execute("DELETE FROM notes_media_cache WHERE notes_file_hash = ?", (notes_file_hash,))
                self.conn.commit()

        # Needs extraction
        staging_dir = self.media_root / f".staging-{uuid.uuid4()}"
        staging_dir.mkdir(parents=True, exist_ok=True)
        
        image_count = 0
        byte_size = 0
        rejected_count = 0
        extracted_sha1s = set()
        
        try:
            doc = docx.Document(str(docx_path))
            for rel in doc.part.related_parts.values():
                if hasattr(rel, 'sha1') and hasattr(rel, 'content_type'):
                    if rel.sha1 in extracted_sha1s:
                        continue
                        
                    ext = _WHITELISTED_CONTENT_TYPES.get(rel.content_type)
                    if not ext:
                        rejected_count += 1
                        continue
                        
                    blob = rel.blob
                    size = len(blob)
                    
                    if size > self.config.max_image_bytes:
                        rejected_count += 1
                        continue
                        
                    if byte_size + size > self.config.max_document_bytes or image_count >= self.config.max_images_per_doc:
                        # Cannot fit, stop trying to extract this part, but don't fail the whole doc
                        rejected_count += 1
                        continue
                        
                    out_path = staging_dir / f"{rel.sha1}{ext}"
                    with open(out_path, "wb") as f:
                        f.write(blob)
                        
                    extracted_sha1s.add(rel.sha1)
                    image_count += 1
                    byte_size += size
        except Exception as e:
            shutil.rmtree(staging_dir, ignore_errors=True)
            logger.error("Extraction failed for %s: %s", docx_path, e)
            return Result(error=ExtractionFailed())
            
        target_dir = self.media_root / notes_file_hash
        
        # 3. Delete row and remove target tree
        cur.execute("DELETE FROM notes_media_cache WHERE notes_file_hash = ?", (notes_file_hash,))
        self.conn.commit()
        shutil.rmtree(target_dir, ignore_errors=True)
        
        # 4. os.replace
        try:
            os.replace(staging_dir, target_dir)
        except OSError as e:
            shutil.rmtree(staging_dir, ignore_errors=True)
            return Result(error=ExtractionFailed())
            
        # 5. Insert row
        cur.execute(
            """
            INSERT INTO notes_media_cache (
                notes_file_hash, extracted_at, last_used_at, byte_size, image_count
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (notes_file_hash, now_iso, now_iso, byte_size, image_count)
        )
        self.conn.commit()
        
        # 6. Evict
        self._evict(now_iso, protect_hash=notes_file_hash)
        
        return Result(value=ExtractionSummary(
            image_count=image_count,
            byte_size=byte_size,
            rejected_count=rejected_count
        ))

    def resolve_image(
        self,
        notes_file_hash: str,
        ref: EcoImageRef
    ) -> Result[CachedImagePath, ImageUnavailable]:
        ext = _WHITELISTED_CONTENT_TYPES.get(ref.content_type)
        if not ext:
            return Result(error=UnsupportedFormat())
            
        img_path = self.media_root / notes_file_hash / f"{ref.blob_sha1}{ext}"
        if not img_path.exists():
            return Result(error=BudgetExceeded())
            
        return Result(value=CachedImagePath(path=img_path, notes_file_hash=notes_file_hash))
