"""Phase 48 section 12.2 -- the shape of the credential defect, kept out.

A default-constructed QSettings with no organisation has no backing store at
all: its fileName() is the empty string, so every read returns the default and
every write goes nowhere. One widget did that, and it made enrichment
unreachable for every operator regardless of what was stored. These are the
grep-shaped tests that stop it recurring the next time a widget needs a
setting.
"""

import pathlib
import re

import pytest

COCKPIT_ROOT = pathlib.Path(__file__).resolve().parent.parent / "cockpit"
CONTROLLER_MODULE = COCKPIT_ROOT / "settings" / "mpn_library.py"

BARE_QSETTINGS = re.compile(r"\bQSettings\(\s*\)")
CREDENTIAL_KEY_LITERALS = (
    "library/digikey_client_id",
    "library/digikey_client_secret",
)


def _python_sources() -> list[pathlib.Path]:
    return sorted(
        path for path in COCKPIT_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def test_no_module_constructs_an_ambient_settings_object():
    offenders = [
        str(path.relative_to(COCKPIT_ROOT))
        for path in _python_sources()
        if BARE_QSETTINGS.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        "A settings object must be injected, never constructed in place: a bare "
        f"QSettings has no backing store. Offenders: {offenders}"
    )


@pytest.mark.parametrize("key_literal", CREDENTIAL_KEY_LITERALS)
def test_credential_key_spellings_live_in_one_place(key_literal):
    """A key rename must not be able to break the enrich path silently while
    the settings dialog keeps working."""
    holders = [
        str(path.relative_to(COCKPIT_ROOT))
        for path in _python_sources()
        if key_literal in path.read_text(encoding="utf-8")
    ]
    assert holders == [str(CONTROLLER_MODULE.relative_to(COCKPIT_ROOT))]


def test_one_store_reaches_both_the_dialog_and_the_enrich_path(tmp_path):
    """Written through one controller over an explicit ini file, resolved
    through another over the same file. Fails against the pre-Phase-48 code,
    where the enrich path read a store that did not exist."""
    from PyQt6.QtCore import QSettings

    from cockpit.settings.mpn_library import Configured, MpnLibrarySettingsController

    ini_path = str(tmp_path / "settings.ini")
    writing_controller = MpnLibrarySettingsController(
        QSettings(ini_path, QSettings.Format.IniFormat)
    )
    writing_controller.set_digikey_credentials("id-1", "secret-1")

    reading_controller = MpnLibrarySettingsController(
        QSettings(ini_path, QSettings.Format.IniFormat)
    )
    stored = reading_controller.digikey_credentials()

    assert isinstance(stored, Configured)
    assert stored.credentials.client_id == "id-1"
    assert stored.credentials.client_secret == "secret-1"
