from dataclasses import dataclass, field
import logging
from pathlib import Path
import sqlite3

from keyring.backends.Windows import WinVaultKeyring

from .files import EXTRACT_ZIPS, FileSafetyError, download_version, validate_destination, version_available
from .itch import ItchClient, ItchError
from .model import Upload, pending_changes
from .store import DownloadRecord, utc_now


SERVICE = "Quick Itch Updater"
LOGGER = logging.getLogger(__name__)


@dataclass
class DownloadResult:
    saved: int = 0
    duplicates: int = 0
    preserved: int = 0
    cleanup_pending: int = 0
    retained_backups: list[Path] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    retryable: list[tuple[int, Upload]] = field(default_factory=list)
    pending: dict = field(default_factory=dict)


@dataclass
class CheckResult:
    pending: dict = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)
    retained_backups: list[Path] = field(default_factory=list)


class Updater:
    def __init__(self, store, credentials=None, client_factory=ItchClient):
        self.store = store
        self.credentials = credentials if credentials is not None else WinVaultKeyring()
        self.client_factory = client_factory

    def connect(self, key):
        with self.client_factory(key) as client:
            account, name = client.profile()
            assets = list(client.owned_assets())
        self.credentials.set_password(SERVICE, str(account), key)
        self.store.import_library(account, assets)
        self.store.set_setting("account", str(account))
        self.store.set_setting(f"name:{account}", name)
        return account, len(assets)

    def client(self, account):
        key = self.credentials.get_password(SERVICE, str(account))
        if not key:
            raise ItchError("Connect your itch.io account in Settings.")
        return self.client_factory(key)

    def refresh_library(self, account):
        with self.client(account) as client:
            if client.profile()[0] != account:
                raise ItchError("The saved key belongs to a different account. Reconnect in Settings.")
            assets = list(client.owned_assets())
        self.store.import_library(account, assets)
        return len(assets)

    def folder(self, row):
        if row.destination:
            return Path(row.destination)
        base = Path(self.store.setting("download_root") or (Path.home() / "GameAssets"))
        return base / row.default_folder

    def _check_asset(self, client, account, asset, retained_backups):
        try:
            retained_backups.extend(self.store.recover_downloads(account, asset.id))
        except OSError:
            raise FileSafetyError("Windows could not recover a previous download. Close files from this pack and retry.") from None
        uploads = client.uploads(asset)
        self.store.save_uploads(account, asset.id, uploads)
        return self._pending(account, asset.id, uploads, checked=True)

    def change_upload_visibility(self, account, asset, visibility):
        self.store.set_upload_visibility(account, asset, visibility)
        return self._pending(account, asset, self.store.uploads(account, asset) or [])

    def _pending(self, account, asset, uploads, *, checked=False):
        saved = {}
        missing = set()
        hidden = self.store.hidden_uploads(account, asset)
        visible = [upload for upload in uploads if upload.id not in hidden]
        wanted = {upload.id: upload.revision for upload in visible}
        versions = self.store.versions(account, asset)
        current_uploads = {version.upload for version in versions if version.current}
        for version in versions:
            if version.upload not in wanted or (version.upload in current_uploads and not version.current):
                continue
            if version.revision == wanted[version.upload] and not version_available(version.path, version.manifest):
                missing.add((version.upload, version.revision))
            else:
                saved.setdefault(version.upload, {})[version.revision] = version.size
        changes = pending_changes(visible, saved, missing)
        date = None
        if not uploads:
            status = "No supported files"
        elif not visible:
            status = "All files hidden"
        elif "Files missing locally" in changes.values():
            status = "Files missing locally"
        elif not saved:
            status = "Ready to download"
        elif any(reason != "Possible change" for reason in changes.values()):
            status = "Update available"
        elif changes:
            status = "Possible change"
        else:
            status = "Up to date"
        update_uploads = [upload for upload, reason in changes.items() if reason != "Files missing locally"]
        if checked and update_uploads and status != "Ready to download":
            first_seen = self.store.setting(f"seen:{account}:{asset}")
            revision_set = repr(sorted(upload.revision for upload in update_uploads))
            if revision_set != first_seen:
                date = max((upload.release_at or utc_now()) if upload.build_id else utc_now()
                           for upload in update_uploads)
                self.store.set_setting(f"seen:{account}:{asset}", revision_set)
        self.store.set_status(account, asset, status, date, checked=checked)
        return changes

    def check(self, account):
        result = CheckResult()
        rows = self.store.library(account, tracked_only=True)
        if not rows:
            return result
        with self.client(account) as client:
            for row in rows:
                try:
                    result.pending[row.asset.id] = self._check_asset(client, account, row.asset, result.retained_backups)
                except (ItchError, FileSafetyError) as error:
                    self.store.set_status(account, row.asset.id, "Check failed")
                    result.errors.append((row.asset.title, str(error)))
                    LOGGER.warning("Update check failed for asset %d", row.asset.id)
        return result

    def download_many(self, account, selections, progress):
        result = DownloadResult()
        groups = {}
        for asset, upload in selections:
            groups.setdefault(asset, {})[upload.id] = upload
        total_files = sum(len(group) for group in groups.values())
        file_number = 0
        rows = {row.asset.id: row for row in self.store.library(account, tracked_only=True)}
        with self.client(account) as client:
            for asset, selected in groups.items():
                row = rows.get(asset)
                if row is None:
                    result.errors.extend((upload.label, "Enable tracking for this pack before downloading.")
                                         for upload in selected.values())
                    result.pending[asset] = {}
                    file_number += len(selected)
                    continue
                try:
                    destination = validate_destination(self.folder(row))
                    changes = self._check_asset(client, account, row.asset, result.retained_backups)
                except (ItchError, FileSafetyError) as error:
                    result.errors.extend((upload.label, str(error)) for upload in selected.values())
                    result.retryable.extend((asset, upload) for upload in selected.values())
                    file_number += len(selected)
                    continue
                except OSError:
                    result.errors.extend((upload.label, "Windows could not verify the download folder.")
                                         for upload in selected.values())
                    result.retryable.extend((asset, upload) for upload in selected.values())
                    file_number += len(selected)
                    continue
                current = {upload.revision: upload for upload in changes}
                mode = row.storage_mode or self.store.setting("storage_mode") or EXTRACT_ZIPS
                for selected_upload in selected.values():
                    file_number += 1
                    upload = current.get(selected_upload.revision)
                    if upload is None:
                        result.errors.append((selected_upload.label,
                                              "This file is hidden, is already saved, or changed on itch.io."))
                        continue
                    progress(file_number, total_files, upload.label, 0, None)
                    try:
                        earlier = [entry for entry in self.store.versions(account, asset)
                                   if entry.upload == upload.id]
                        download = download_version(
                            client, upload, destination, earlier,
                            lambda received, total: progress(file_number, total_files, upload.label, received, total),
                            mode, record=DownloadRecord(self.store, account, asset, upload),
                        )
                    except (ItchError, FileSafetyError) as error:
                        message = str(error)
                    except OSError:
                        message = "Windows could not save or extract this file."
                    except sqlite3.Error:
                        message = "Could not record the download. Check for updates and retry."
                    else:
                        result.saved += int(download.created)
                        result.duplicates += int(not download.created)
                        result.preserved += int(download.preserved)
                        result.cleanup_pending += int(download.cleanup_pending)
                        if download.retained_backup is not None:
                            result.retained_backups.append(download.retained_backup)
                        continue
                    result.errors.append((upload.label, message))
                    result.retryable.append((asset, upload))
                result.pending[asset] = self._pending(account, asset, self.store.uploads(account, asset))
        return result
