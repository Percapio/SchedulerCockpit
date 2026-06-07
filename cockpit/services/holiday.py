from datetime import date
from typing import Set
import sqlite3

class HolidayRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def list(self) -> Set[date]:
        cur = self.conn.cursor()
        cur.execute("SELECT holiday_date FROM holidays")
        return {date.fromisoformat(row["holiday_date"]) for row in cur.fetchall()}

    def add(self, d: date) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO holidays (holiday_date) VALUES (?)",
            (d.isoformat(),)
        )

    def remove(self, d: date) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "DELETE FROM holidays WHERE holiday_date = ?",
            (d.isoformat(),)
        )

class HolidayService:
    def __init__(self, repo: HolidayRepository):
        self._repo = repo

    def list_holidays(self) -> Set[date]:
        return self._repo.list()

    def add_holiday(self, d: date) -> None:
        self._repo.add(d)

    def remove_holiday(self, d: date) -> None:
        self._repo.remove(d)
