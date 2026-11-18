from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .files import ExtractedManifest, FileSafetyError, matches_download, recover_replacement
from .model import Asset, Upload


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assets (
    account INTEGER NOT NULL, id INTEGER NOT NULL, title TEXT NOT NULL, creator TEXT NOT NULL,
    url TEXT NOT NULL, key_ids TEXT NOT NULL, classification TEXT,
    tracked INTEGER NOT NULL DEFAULT 0, destination TEXT, status TEXT, last_checked TEXT,
    last_update TEXT, available INTEGER NOT NULL DEFAULT 1, storage_mode TEXT,
    PRIMARY KEY (account, id)
);
CREATE TABLE IF NOT EXISTS versions (
    account INTEGER NOT NULL, asset INTEGER NOT NULL, upload INTEGER NOT NULL,
    revision TEXT NOT NULL, size INTEGER, digest TEXT NOT NULL, path TEXT NOT NULL,
    saved_at TEXT NOT NULL, manifest TEXT, current INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account, asset, upload, revision)
);
CREATE TABLE IF NOT EXISTS pending_replacements (
    account INTEGER NOT NULL, asset INTEGER NOT NULL, upload INTEGER NOT NULL,
    root TEXT NOT NULL, backup TEXT NOT NULL, committed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account, asset, upload)
);
CREATE TABLE IF NOT EXISTS upload_catalog (
    account INTEGER NOT NULL, asset INTEGER NOT NULL, uploads TEXT NOT NULL,
    PRIMARY KEY (account, asset)
);
CREATE TABLE IF NOT EXISTS hidden_uploads (
    account INTEGER NOT NULL, asset INTEGER NOT NULL, upload INTEGER NOT NULL,
    PRIMARY KEY (account, asset, upload)
);
"""


@dataclass(frozen=True)
class LibraryRow:
    asset: Asset
    tracked: bool
    destination: str | None
    status: str | None
    last_checked: str | None
    last_update: str | None
    available: bool
    storage_mode: str | None


@dataclass(frozen=True)
class Version:
    upload: int
    revision: str
    size: int | None
    digest: str
    path: Path
    saved_at: str
    manifest: ExtractedManifest | None
    current: bool


@dataclass(frozen=True)
class DownloadRecord:
    store: "Store"
    account: int
    asset: int
    upload: Upload

    def begin(self, root, backup):
        with self.store.connect() as connection:
            connection.execute("INSERT INTO pending_replacements (account, asset, upload, root, backup) VALUES (?, ?, ?, ?, ?)",
                               (self.account, self.asset, self.upload.id, str(root), str(backup)))

    def save(self, download):
        self.store.save_version(self.account, self.asset, self.upload, download)

    def finish(self):
        self.store.clear_replacement(self.account, self.asset, self.upload.id)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, database):
        self.database = database
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            for table, column, definition in (("assets", "storage_mode", "TEXT"), ("versions", "manifest", "TEXT"),
                                              ("versions", "current", "INTEGER NOT NULL DEFAULT 0")):
                columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def setting(self, name):
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    def set_setting(self, name, value):
        with self.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (name, value))

    def import_library(self, account, assets):
        with self.connect() as connection:
            connection.execute("UPDATE assets SET available = 0 WHERE account = ?", (account,))
            connection.executemany("""
                INSERT INTO assets (account, id, title, creator, url, key_ids, classification)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account, id) DO UPDATE SET title = excluded.title,
                    creator = excluded.creator, url = excluded.url, key_ids = excluded.key_ids,
                    classification = excluded.classification, available = 1
            """, [(account, a.id, a.title, a.creator, a.url, json.dumps(a.key_ids), a.classification)
                  for a in assets])

    def library(self, account, tracked_only=False):
        query = "SELECT * FROM assets WHERE account = ?"
        if tracked_only:
            query += " AND tracked = 1 AND available = 1"
        query += " ORDER BY title COLLATE NOCASE, id"
        with self.connect() as connection:
            rows = connection.execute(query, (account,)).fetchall()
        return [LibraryRow(
            Asset(r["id"], r["title"], r["creator"], r["url"], tuple(json.loads(r["key_ids"])), r["classification"]),
            bool(r["tracked"]), r["destination"], r["status"], r["last_checked"], r["last_update"],
            bool(r["available"]), r["storage_mode"],
        ) for r in rows]

    def track(self, account, asset, enabled):
        with self.connect() as connection:
            connection.execute("UPDATE assets SET tracked = ?, status = NULL WHERE account = ? AND id = ?",
                               (enabled, account, asset))

    def set_pack_settings(self, account, asset, path, mode):
        with self.connect() as connection:
            connection.execute("UPDATE assets SET destination = ?, storage_mode = ? WHERE account = ? AND id = ?",
                               (path, mode, account, asset))

    def set_status(self, account, asset, status, last_update=None, *, checked=True):
        with self.connect() as connection:
            connection.execute("""
                UPDATE assets SET status = ?, last_checked = COALESCE(?, last_checked),
                    last_update = COALESCE(?, last_update)
                WHERE account = ? AND id = ?
            """, (status, utc_now() if checked else None, last_update, account, asset))

    def save_uploads(self, account, asset, uploads):
        with self.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO upload_catalog VALUES (?, ?, ?)",
                               (account, asset, json.dumps([asdict(upload) for upload in uploads])))

    def uploads(self, account, asset):
        with self.connect() as connection:
            row = connection.execute("SELECT uploads FROM upload_catalog WHERE account = ? AND asset = ?",
                                     (account, asset)).fetchone()
        return [Upload(**data) for data in json.loads(row[0])] if row else None

    def hidden_uploads(self, account, asset):
        with self.connect() as connection:
            rows = connection.execute("SELECT upload FROM hidden_uploads WHERE account = ? AND asset = ?",
                                      (account, asset)).fetchall()
        return {row[0] for row in rows}

    def set_upload_visibility(self, account, asset, changes):
        with self.connect() as connection:
            connection.executemany("DELETE FROM hidden_uploads WHERE account = ? AND asset = ? AND upload = ?",
                                   [(account, asset, upload) for upload, visible in changes.items() if visible])
            connection.executemany("INSERT OR IGNORE INTO hidden_uploads VALUES (?, ?, ?)",
                                   [(account, asset, upload) for upload, visible in changes.items() if not visible])

    def versions(self, account, asset):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT * FROM versions WHERE account = ? AND asset = ? ORDER BY saved_at DESC, rowid DESC
            """, (account, asset)).fetchall()
        return [Version(row["upload"], row["revision"], row["size"], row["digest"], Path(row["path"]),
                        row["saved_at"], ExtractedManifest(**json.loads(row["manifest"])) if row["manifest"] else None,
                        bool(row["current"]))
                for row in rows]

    def recover_downloads(self, account, asset):
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM pending_replacements WHERE account = ? AND asset = ?",
                                      (account, asset)).fetchall()
        for row in rows:
            root, backup = Path(row["root"]), Path(row["backup"])
            if row["committed"] and backup.exists():
                version = next((item for item in self.versions(account, asset)
                                if item.current and item.upload == row["upload"]), None)
                if version is None or not matches_download(root, version):
                    raise FileSafetyError("A previous download backup was kept because the current folder changed.")
            recover_replacement(root, backup, bool(row["committed"]))
            self.clear_replacement(account, asset, row["upload"])

    def clear_replacement(self, account, asset, upload):
        with self.connect() as connection:
            connection.execute("DELETE FROM pending_replacements WHERE account = ? AND asset = ? AND upload = ?",
                               (account, asset, upload))

    def save_version(self, account, asset, upload, download):
        manifest = json.dumps(asdict(download.manifest)) if download.manifest is not None else None
        with self.connect() as connection:
            if download.current:
                if download.replaced or not download.created:
                    connection.execute("DELETE FROM versions WHERE account = ? AND asset = ? AND upload = ? AND current = 1",
                                       (account, asset, upload.id))
                else:
                    connection.execute("UPDATE versions SET current = 0 WHERE account = ? AND asset = ? AND upload = ? AND current = 1",
                                       (account, asset, upload.id))
            connection.execute("""
                INSERT OR REPLACE INTO versions (account, asset, upload, revision, size, digest, path, saved_at, manifest, current)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (account, asset, upload.id, upload.revision, upload.size, download.digest,
                  str(download.path), utc_now(), manifest, download.current))
            connection.execute("UPDATE pending_replacements SET committed = 1 WHERE account = ? AND asset = ? AND upload = ?",
                               (account, asset, upload.id))
