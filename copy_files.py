"""
copy_files.py
-------------
Copies the latest Traveler (Excel), AUDIT BOM (Excel), and ECO (Word) files
from each job folder in E:\\BACKUP FOR JOB FOLDERS whose job number falls
between B136031 and B139135 (inclusive) into backend\\data, preserving the
source folder structure.

Overwrites are allowed and recorded in logs.txt alongside this script.
"""

import os
import re
import shutil
import logging
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

SOURCE_ROOT = Path(r"E:\BACKUP FOR JOB FOLDERS")
DEST_ROOT   = Path(__file__).parent / "backend" / "data"
LOG_FILE    = Path(__file__).parent / "logs.txt"

START_JOB = 136031
END_JOB   = 139135

EXCEL_EXTENSIONS = {".xlsx", ".xls", ".xlsm"}
WORD_EXTENSIONS  = {".docx", ".doc"}

# Matches folder names that begin with B followed by 6+ digits (e.g. "B136031 Job Name")
JOB_FOLDER_RE = re.compile(r"^B(\d{6,})", re.IGNORECASE)

# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging() -> None:
    logging.basicConfig(
        filename=str(LOG_FILE),
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_job_number(folder_name: str) -> int | None:
    """Return the numeric job number from a folder name, or None if not a job folder."""
    match = JOB_FOLDER_RE.match(folder_name)
    return int(match.group(1)) if match else None


def get_latest_file(directory: Path, name_fragment: str, extensions: set[str]) -> Path | None:
    """
    Return the most recently modified file in *directory* (non-recursive) whose
    name contains *name_fragment* (case-insensitive) and whose extension is in
    *extensions*.  Returns None when no match exists.
    """
    candidates = [
        entry
        for entry in directory.iterdir()
        if entry.is_file()
        and entry.suffix.lower() in extensions
        and name_fragment.lower() in entry.name.lower()
    ]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def copy_with_logging(src: Path, dest: Path) -> None:
    """Copy *src* to *dest*, creating parent directories as needed.  Logs overwrites."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        logging.warning("OVERWRITE  %s  ->  %s", src, dest)
        print(f"    [OVERWRITE] {dest.relative_to(DEST_ROOT)}")
    else:
        logging.info("COPIED     %s  ->  %s", src, dest)
        print(f"    [COPIED]    {dest.relative_to(DEST_ROOT)}")
    shutil.copy2(src, dest)


# ── Core logic ────────────────────────────────────────────────────────────────

def find_job_folders(root: Path):
    """
    Walk *root* and yield every directory whose name encodes a job number in
    [START_JOB, END_JOB].  Does not descend into matched job folders.
    """
    for dirpath, dirnames, _ in os.walk(root):
        current = Path(dirpath)
        job_num = extract_job_number(current.name)
        if job_num is not None and START_JOB <= job_num <= END_JOB:
            yield current
            dirnames.clear()   # do not recurse into sub-folders of a job folder
        else:
            dirnames.sort()    # keep traversal order deterministic


def process_job_folder(job_folder: Path) -> None:
    """Copy the latest Traveler, AUDIT BOM, and ECO files from *job_folder*."""
    rel      = job_folder.relative_to(SOURCE_ROOT)
    dest_dir = DEST_ROOT / rel

    targets = [
        ("Traveler",  EXCEL_EXTENSIONS),
        ("AUDIT BOM", EXCEL_EXTENSIONS),
        ("ECO",       WORD_EXTENSIONS),
    ]

    any_found = False
    for name_fragment, extensions in targets:
        latest = get_latest_file(job_folder, name_fragment, extensions)
        if latest:
            copy_with_logging(latest, dest_dir / latest.name)
            any_found = True
        else:
            logging.info("NOT FOUND  [%s]  in  %s", name_fragment, job_folder)

    if not any_found:
        print("    (no matching files)")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()

    print(f"Source : {SOURCE_ROOT}")
    print(f"Dest   : {DEST_ROOT}")
    print(f"Log    : {LOG_FILE}")
    print(f"Range  : B{START_JOB} – B{END_JOB}")
    print()

    job_folders = sorted(find_job_folders(SOURCE_ROOT))

    if not job_folders:
        print("No job folders found in the specified range.  Check SOURCE_ROOT.")
        return

    print(f"Found {len(job_folders)} job folder(s) in range.\n")

    for job_folder in job_folders:
        print(f"  {job_folder.name}")
        process_job_folder(job_folder)

    print(f"\nDone.  Full log written to {LOG_FILE}")


if __name__ == "__main__":
    main()
