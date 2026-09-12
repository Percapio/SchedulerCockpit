from PyQt6.QtCore import QObject, QSettings, pyqtSignal

class MpnLibrarySettingsController(QObject):
    changed = pyqtSignal()

    def __init__(self, settings: QSettings):
        super().__init__()
        self._settings = settings

    def is_enabled(self) -> bool:
        # Default is False
        val = self._settings.value("library/enabled", False, type=bool)
        return val

    def set_enabled(self, enabled: bool):
        self._settings.setValue("library/enabled", enabled)
        self.changed.emit()

    def stale_after_days(self) -> int:
        # Default is 180
        val = self._settings.value("library/stale_after_days", 180, type=int)
        return val

    def set_stale_after_days(self, days: int):
        self._settings.setValue("library/stale_after_days", days)
        self.changed.emit()

    def get_digikey_credentials(self):
        client_id = self._settings.value("library/digikey_client_id", "", type=str)
        client_secret = self._settings.value("library/digikey_client_secret", "", type=str)
        if client_id and client_secret:
            from cockpit.services.mpn_library.digikey_types import DigiKeyCredentials
            return DigiKeyCredentials(client_id=client_id, client_secret=client_secret)
        return None

    def set_digikey_credentials(self, client_id: str, client_secret: str):
        self._settings.setValue("library/digikey_client_id", client_id)
        self._settings.setValue("library/digikey_client_secret", client_secret)
        self.changed.emit()
        
    def forget_digikey_credentials(self):
        self._settings.remove("library/digikey_client_id")
        self._settings.remove("library/digikey_client_secret")
        self.changed.emit()
