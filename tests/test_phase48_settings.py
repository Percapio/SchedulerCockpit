"""Phase 48 section 12.3 -- the MPN library settings contract.

Pure service code with no widget, so the whole contract is covered before any
dialog exists to exercise it.
"""

import pytest
from PyQt6.QtCore import QSettings

from cockpit.settings.mpn_library import (
    Accepted,
    BaseUrlFault,
    Configured,
    MissingCredentialField,
    MpnLibrarySettingsController,
    Partial,
    Rejected,
    Unset,
    min_call_ceiling,
    production_api_base_url,
    validate_api_base_url,
)


@pytest.fixture
def controller(tmp_path):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    return MpnLibrarySettingsController(settings)


# --- credentials ------------------------------------------------------------

def test_credentials_states(controller):
    assert isinstance(controller.digikey_credentials(), Unset)

    controller._settings.setValue("library/digikey_client_id", "id-1")
    stored = controller.digikey_credentials()
    assert isinstance(stored, Partial)
    assert stored.missing == MissingCredentialField.CLIENT_SECRET

    controller._settings.remove("library/digikey_client_id")
    controller._settings.setValue("library/digikey_client_secret", "secret-1")
    stored = controller.digikey_credentials()
    assert isinstance(stored, Partial)
    assert stored.missing == MissingCredentialField.CLIENT_ID

    controller._settings.setValue("library/digikey_client_id", "id-1")
    stored = controller.digikey_credentials()
    assert isinstance(stored, Configured)
    assert stored.credentials.client_id == "id-1"
    assert stored.credentials.client_secret == "secret-1"


def test_partial_carries_no_values(controller):
    controller._settings.setValue("library/digikey_client_secret", "secret-1")
    stored = controller.digikey_credentials()
    assert "secret-1" not in repr(stored)


def test_trailing_newline_is_trimmed(controller):
    """A client id pasted from a web console carries a trailing newline often
    enough that an untrimmed value reaches the token endpoint and is rejected."""
    controller._settings.setValue("library/digikey_client_id", "id-1\n")
    controller._settings.setValue("library/digikey_client_secret", "  secret-1  ")

    stored = controller.digikey_credentials()
    assert isinstance(stored, Configured)
    assert stored.credentials.client_id == "id-1"
    assert stored.credentials.client_secret == "secret-1"


def test_set_credentials_emits_changed_once_for_the_pair(controller):
    emissions = []
    controller.changed.connect(lambda: emissions.append(1))
    controller.set_digikey_credentials("id-1", "secret-1")
    assert len(emissions) == 1


def test_set_client_id_leaves_the_secret_byte_identical(controller):
    controller.set_digikey_credentials("id-1", "secret-1")
    controller.set_digikey_client_id("id-2")

    stored = controller.digikey_credentials()
    assert stored.credentials.client_id == "id-2"
    assert stored.credentials.client_secret == "secret-1"


def test_set_client_id_refuses_when_no_secret_is_stored(controller):
    with pytest.raises(ValueError):
        controller.set_digikey_client_id("id-1")


def test_forget_leaves_every_other_library_key_untouched(controller):
    controller.set_enabled(True)
    controller.set_stale_after_days(42)
    controller.set_api_base_url("https://sandbox-api.digikey.com")
    controller.set_call_ceiling(60)
    controller.set_digikey_credentials("id-1", "secret-1")

    controller.forget_digikey_credentials()

    assert isinstance(controller.digikey_credentials(), Unset)
    assert controller.is_enabled() is True
    assert controller.stale_after_days() == 42
    assert controller.api_base_url() == Accepted("https://sandbox-api.digikey.com")
    assert controller.call_ceiling() == 60


def test_forget_works_from_partial(controller):
    controller._settings.setValue("library/digikey_client_id", "id-1")
    controller.forget_digikey_credentials()
    assert isinstance(controller.digikey_credentials(), Unset)


# --- base URL ---------------------------------------------------------------

@pytest.mark.parametrize("raw,fault", [
    ("http://api.digikey.com", BaseUrlFault.NON_HTTPS_SCHEME),
    ("https://id:pw@api.digikey.com", BaseUrlFault.USERINFO_PRESENT),
    ("https:///v4", BaseUrlFault.NO_HOST),
    ("https://api.digikey.com?tenant=2", BaseUrlFault.QUERY_OR_FRAGMENT_PRESENT),
    ("https://api.digikey.com#frag", BaseUrlFault.QUERY_OR_FRAGMENT_PRESENT),
    ("api.digikey.com", BaseUrlFault.MALFORMED_URL),
    ("", BaseUrlFault.MALFORMED_URL),
])
def test_base_url_faults(raw, fault):
    assert validate_api_base_url(raw) == Rejected(fault)


def test_base_url_accepts_production_and_sandbox_and_strips_trailing_slash():
    assert validate_api_base_url("https://api.digikey.com") == Accepted("https://api.digikey.com")
    assert validate_api_base_url("https://sandbox-api.digikey.com/") == Accepted(
        "https://sandbox-api.digikey.com"
    )


def test_two_faults_report_the_earlier_one_in_the_declared_order():
    assert validate_api_base_url("http://id:pw@host") == Rejected(BaseUrlFault.NON_HTTPS_SCHEME)


def test_unset_base_url_defaults_to_production(controller):
    assert controller.api_base_url() == Accepted(production_api_base_url())


def test_rejected_base_url_never_falls_back_to_production(controller):
    """Falling back would mean an operator who mistyped a sandbox host
    silently spends production quota."""
    controller.set_api_base_url("http://api.digikey.com")

    resolved = controller.api_base_url()
    assert isinstance(resolved, Rejected)
    assert resolved.fault == BaseUrlFault.NON_HTTPS_SCHEME


# --- call ceiling -----------------------------------------------------------

def test_call_ceiling_default_is_the_previously_hardcoded_value(controller):
    assert controller.call_ceiling() == 150


def test_call_ceiling_below_the_floor_is_refused(controller):
    """The floor is MAX_RETRIES + 2: one mint, one request, and up to
    MAX_RETRIES re-mints. Below it a rejected token reports QuotaExhausted."""
    with pytest.raises(ValueError):
        controller.set_call_ceiling(min_call_ceiling() - 1)

    controller.set_call_ceiling(min_call_ceiling())
    assert controller.call_ceiling() == min_call_ceiling()
