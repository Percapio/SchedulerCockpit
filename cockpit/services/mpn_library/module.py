import threading
from pathlib import Path
from .connection import open_library_connection, LibraryConnection
from .repository import LibraryRepository
from .schema import migrate_library

class MpnLibraryModule:
    """
    Container for the MPN library's live dependencies.
    Ties together the connection, repository, and teardown logic.
    """
    def __init__(self, db_path: Path):
        self.db_path = db_path
        
        # 1. Migrate the database (runs in its own short-lived connections)
        migrate_library(db_path)
        
        # 2. Open ui_conn bound to the thread that constructed this module
        self.ui_conn: LibraryConnection = open_library_connection(db_path, threading.get_ident())
        
        # 3. Initialize repository
        self.repository = LibraryRepository(self.ui_conn)
        
    def teardown(self):
        """
        Module released sequence.
        Closes ui_conn deterministically.
        """
        if self.ui_conn:
            self.ui_conn.close()
            self.ui_conn = None
