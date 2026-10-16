
import sqlite3
from pathlib import Path

db_path = Path(__file__).resolve().parent / "websites.db"

with sqlite3.connect(db_path) as conn:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS uptime_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            status TEXT NOT NULL,
            http_status INTEGER,
            response_time_ms REAL,
            message TEXT,
            checked_at TEXT NOT NULL
        )
    """)

    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(uptime_checks)")
    }

    if "message" not in columns:
        conn.execute(
            "ALTER TABLE uptime_checks ADD COLUMN message TEXT"
        )

    if "error" not in columns:
        conn.execute(
            "ALTER TABLE uptime_checks ADD COLUMN error TEXT"
        )

    conn.commit()

print("Uptime database table checked and repaired.")
