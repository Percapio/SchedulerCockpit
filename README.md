# Cockpit — Manufacturing Audit Tool

Cockpit is a local desktop app for electronics manufacturing. You drop three files onto it — an **Audit BOM** (what parts go on the board), a **Shop Traveler** (job routing info), and **Build Notes** (a Word doc with extra steps) — and it builds an interactive checklist that ties everything together. No server, no internet, just a window on your machine.

---

Cockpit runs out of the box with zero installation required, but relies on your personal AppData folder to store audits.

### Running on restricted machines

If you are running Cockpit on a heavily restricted work machine where writing to the standard application data folder is denied by policy, Cockpit will automatically fall back to storing your data in a local `data/` folder directly next to the `Cockpit.exe` application file (or in the project root if running from source).

To manually override where Cockpit stores its data (for example, to store audits on a portable drive), you can set the `COCKPIT_APP_DATA` environment variable before launching the application:

```cmd
set COCKPIT_APP_DATA=D:\MyCustomFolder
Cockpit.exe
```

---

## Table of Contents

1. [What it does](#what-it-does)
2. [Setting up a dev environment](#setting-up-a-dev-environment)
3. [Running the app from source](#running-the-app-from-source)
4. [Running tests](#running-tests)
5. [Building the executable](#building-the-executable)

---

## What it does

- **Drag-and-drop ingestion** — drop an Audit BOM (`.xlsx`), a Traveler (`.xlsx`), and Build Notes (`.docx`) onto the window at the same time.
- **Gatekeeper check** — files are rejected if their names don't contain "Audit BOM" and "Traveler". Garbage in, hard stop out.
- **Checklist generation** — through-hole parts from the BOM become verification checklist rows; Build Notes tables become step-by-step checklist items.
- **Job splitting** — a job can be split into sibling work orders (e.g. `Job-A`, `Job-B`) with separate quantities.
- **Cascade deletion** — when a job completes, all uploaded files and database records are permanently deleted. Files shared between split jobs are only deleted when every sibling is done.
- **Local SQLite database** — all state lives in `local_audit.db` on disk. Nothing leaves the machine.

---

## Setting up a dev environment

You need **Python 3.11 or newer**. Older versions are not tested.

```powershell
# 1. Clone the repo and enter the folder
cd d:\Dev\Scheduler\Cockpit

# 2. Create a virtual environment (one-time setup)
python -m venv venv

# 3. Activate it
venv\Scripts\Activate.ps1      # PowerShell
# or
venv\Scripts\activate.bat      # cmd.exe
# or
.\venv\Scripts\activate

# 4. Install runtime dependencies
pip install -r requirements.txt

# 5. Install build tools (only needed if you plan to make an .exe)
pip install -r requirements-build.txt
```

### Dependencies at a glance

| Package | Why |
|---|---|
| `PyQt6` | The desktop window and widgets |
| `openpyxl` | Reads `.xlsx` BOM and Traveler files |
| `python-docx` | Reads `.docx` Build Notes |
| `PyMuPDF` | PDF handling |
| `jsonschema` | Validates config files at startup |
| `pytest` / `pytest-qt` | Test runner |

---

## Running the app from source

### After a rebuild that changes the database schema

The app applies schema migrations automatically on startup, so additive changes (new columns, new tables) require no manual steps. If a rebuild makes a **breaking** schema change — such as renaming or dropping a column, changing a constraint, or restructuring a table — the existing database must be deleted before launching, because the migration system only runs forward.

Delete the database file:

```powershell
# Default location on Windows
Remove-Item "$env:APPDATA\Cockpit\v1\local_audit.db"
```

If you are using the `COCKPIT_APP_DATA` override or the local `data/` fallback, the file is at `<your-data-root>\v1\local_audit.db` instead.

> **Warning:** Deleting the database permanently removes all active audit records and uploaded file references stored in it. The uploaded files themselves live under `uploads\` in the same folder and are **not** deleted by this step — you can remove those manually as well if a clean slate is needed.

### Normal launch

With the virtual environment active:

```powershell
python cockpit_main.py
```

---

## Running tests

All tests live under `tests/`. There are two suites:

| Suite | What it covers |
|---|---|
| `tests/services/` | Ingestion pipeline, completion logic, database operations |
| `tests/ui/` | Widget rendering, theme loading, crash reporter, data migration |

### Run everything

```powershell
pytest
```

### Run only the fast service tests (no Qt window needed)

```powershell
pytest tests/services/
```

### Run only UI tests

```powershell
pytest tests/ui/
```

### Run a single test file

```powershell
pytest tests/ui/test_theme.py
```

### Helpful flags

```powershell
pytest -v          # verbose — shows each test name as it runs
pytest -x          # stop on the first failure
pytest --tb=short  # shorter tracebacks
```

> **Note:** UI tests spin up a real (hidden) Qt application. If you see an error about a display, make sure you're running in a graphical session, not a headless SSH shell.

---

## Building the executable

The app is packaged with **PyInstaller** into a folder-based distribution (`--onedir`). The spec file at `cockpit.spec` controls everything.

```powershell
# Make sure build tools are installed
pip install -r requirements-build.txt

# Build
pyinstaller cockpit.spec
```

Output lands in `dist\Cockpit\`. The entry point is `dist\Cockpit\Cockpit.exe`.

### What the build bundles

- All Python source under `cockpit/`
- Theme config (`cockpit/ui/theme.json`, `cockpit/ui/theme.schema.json`)
- The traveler coordinate map (`cockpit/ingestion/config/default_traveler_map.json`)
- Only the PyQt6 plugins that are actually used (`platforms`, `styles`, `imageformats`)

### Smoke-test the binary

Run the exe with the bootstrap flag to verify it launches and shuts down cleanly without needing real files:

```powershell
.\dist\Cockpit\Cockpit.exe --smoke-exit-after-bootstrap
```

A zero exit code means the binary is healthy.
