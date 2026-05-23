"""
copy_smt_pdfs.py
----------------
For every job folder already present under backend\\data, find the
corresponding job folder in E:\\BACKUP FOR JOB FOLDERS and copy any file
whose name matches *_SMT.pdf (case-insensitive) into the backend\\data
counterpart.

Conflict rule: when the destination file already exists, keep whichever
copy has the later modification time.  Both cases are logged to logs.txt
for manual review.

Missing source folders or jobs with no *_SMT.pdf are reported to stdout
and logged.
"""

import os
import shutil
import logging
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

SOURCE_ROOT = Path(r"E:\BACKUP FOR JOB FOLDERS")
DEST_ROOT   = Path(__file__).parent / "backend" / "data"
LOG_FILE    = Path(__file__).parent / "logs.txt"

# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging() -> None:
    logging.basicConfig(
        filename=str(LOG_FILE),
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_smt_pdf(filename: str) -> bool:
    """Return True when *filename* (any case) ends with _SMT.pdf."""
    return filename.lower().endswith("_smt.pdf")


def find_smt_pdfs(directory: Path) -> list[Path]:
    """Return all *_SMT.pdf files (non-recursive) in *directory*."""
    if not directory.is_dir():
        return []
    return [entry for entry in directory.iterdir()
            if entry.is_file() and is_smt_pdf(entry.name)]


def copy_with_conflict_check(src: Path, dest: Path) -> None:
    """
    Copy *src* to *dest*.  When *dest* already exists, compare modification
    times: whichever file is newer is kept.  The conflict is always logged.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        src_mtime  = src.stat().st_mtime
        dest_mtime = dest.stat().st_mtime

        if src_mtime > dest_mtime:
            logging.warning(
                "CONFLICT   source is newer — overwriting  %s  ->  %s", src, dest
            )
            print(f"    [CONFLICT] source newer, overwriting: {dest.name}")
            shutil.copy2(src, dest)
        else:
            logging.warning(
                "CONFLICT   dest is newer — keeping dest  %s  (skipped %s)", dest, src
            )
            print(f"    [CONFLICT] dest newer, keeping:     {dest.name}")
    else:
        logging.info("COPIED     %s  ->  %s", src, dest)
        print(f"    [COPIED]    {dest.name}")
        shutil.copy2(src, dest)


# ── Core logic ────────────────────────────────────────────────────────────────

def job_folders_from_dest(root: Path):
    """
    Yield every leaf directory under *root* that is a job folder
    (name starts with B followed by digits, e.g. "B139104 Some Name").
    Iterates depth-first; does not yield intermediate grouping folders.
    """
    for dirpath, dirnames, _ in os.walk(root):
        current = Path(dirpath)
        # A job folder has a name like B139104 Some Name — 7-char prefix "B######"
        if len(current.name) >= 7 and current.name[0].upper() == "B" and current.name[1:7].isdigit():
            yield current
            dirnames.clear()   # do not recurse into sub-folders of a job folder
        else:
            dirnames.sort()    # deterministic traversal order


def process_job_folder(dest_job_folder: Path) -> None:
    """
    Resolve the matching source folder, find *_SMT.pdf files, and copy them
    to *dest_job_folder* applying the keep-newest conflict rule.
    """
    rel              = dest_job_folder.relative_to(DEST_ROOT)
    source_job_folder = SOURCE_ROOT / rel

    if not source_job_folder.exists():
        msg = f"SOURCE NOT FOUND  {source_job_folder}"
        logging.info(msg)
        print(f"  [MISSING SOURCE] {rel}")
        return

    smt_pdfs = find_smt_pdfs(source_job_folder)

    if not smt_pdfs:
        msg = f"NOT FOUND  [*_SMT.pdf]  in  {source_job_folder}"
        logging.info(msg)
        print(f"  [NO SMT PDF]     {rel}")
        return

    print(f"  {dest_job_folder.name}")
    for pdf in sorted(smt_pdfs):
        copy_with_conflict_check(pdf, dest_job_folder / pdf.name)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()

    print(f"Source : {SOURCE_ROOT}")
    print(f"Dest   : {DEST_ROOT}")
    print(f"Log    : {LOG_FILE}")
    print(f"Pattern: *_SMT.pdf (case-insensitive)")
    print()

    job_folders = sorted(job_folders_from_dest(DEST_ROOT))

    if not job_folders:
        print("No job folders found under DEST_ROOT.  Check path.")
        return

    print(f"Found {len(job_folders)} job folder(s) in backend/data.\n")

    for dest_job_folder in job_folders:
        process_job_folder(dest_job_folder)

    print(f"\nDone.  Full log written to {LOG_FILE}")


if __name__ == "__main__":
    main()
