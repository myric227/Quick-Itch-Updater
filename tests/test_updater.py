from contextlib import contextmanager
import io
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import httpx

from quick_itch.files import KEEP_ARCHIVES, FileSafetyError, download_version, extract_zip, validate_destination
from quick_itch.itch import ItchClient, ItchError
from quick_itch.model import Asset, Upload, pending_changes
from quick_itch.store import DownloadRecord, Store
from quick_itch.updater import CheckResult, Updater


def archive_bytes(name="asset.txt", contents=b"asset", compression=ZIP_STORED):
    stream = io.BytesIO()
    with ZipFile(stream, "w", compression=compression) as archive:
        archive.writestr(name, contents)
    return stream.getvalue()


def hosted_upload(data, timestamp="2026-11-18T00:00:00Z"):
    return Upload(45, "asset.zip", len(data), "hosted", 99, updated_at=timestamp)


class Transfer:
    def __init__(self, data, interrupted=False):
        self.data = data
        self.interrupted = interrupted
        self.headers = {"Content-Length": str(len(data))}

    @contextmanager
    def open_download(self, upload):
        yield self

    def iter_raw(self, chunk_size):
        yield self.data
        if self.interrupted:
            raise ItchError("Interrupted")


class UpdateTests(unittest.TestCase):
    def test_revision_comparison(self):
        build = Upload(5, "file.zip", 100, "build", 99, build_id=20)
        metadata_only = Upload(5, "renamed.zip", 100, "build", 99, build_id=20, updated_at="later")
        newer = Upload(5, "file.zip", 100, "build", 99, build_id=21)
        self.assertEqual(pending_changes([metadata_only, newer], {5: {build.revision: 100}}),
                         {newer: "Update available"})
        original = hosted_upload(b"abc")
        uncertain = hosted_upload(b"xyz", "later")
        changed_size = hosted_upload(b"xyz!", "later")
        self.assertEqual(pending_changes([original, uncertain, changed_size], {45: {original.revision: 3}}),
                         {uncertain: "Possible change", changed_size: "Update available"})

    def test_only_tracked_assets_get_upload_requests(self):
        requests = []

        def handler(request):
            requests.append(request.url.path)
            self.assertEqual(request.headers.get("Authorization"), "Bearer test-key")
            return httpx.Response(200, json={"uploads": []})

        class Credentials:
            def get_password(self, service, account):
                return "test-key"

        with TemporaryDirectory() as temp:
            database = Path(temp) / "library.db"
            store = Store(database)
            assets = [Asset(i, str(i), "Creator", "", (i,)) for i in range(1, 101)]
            store.import_library(1, assets)
            self.assertFalse(any(row.tracked for row in store.library(1)))
            for asset_id in (5, 8, 12):
                store.track(1, asset_id, True)
            store.import_library(1, assets)
            reopened = Store(database)
            updater = Updater(reopened, Credentials(), lambda key: ItchClient(key, httpx.MockTransport(handler)))
            result = updater.check(1)
            self.assertFalse(result.errors)
            self.assertEqual(set(result.pending), {5, 8, 12})
            self.assertCountEqual(requests, [f"/games/{i}/uploads" for i in (5, 8, 12)])
            self.assertTrue(all(row.last_checked for row in reopened.library(1, tracked_only=True)))
            reopened.track(1, 5, False)
            result = updater.download_many(1, [(5, hosted_upload(b"abc"))], lambda *_: None)
            self.assertIn("Enable tracking", result.errors[0][1])
            self.assertEqual(result.saved, 0)

    def test_owned_key_pagination_and_deduplication(self):
        game = {"title": "Pack", "url": "https://creator.itch.io/asset"}
        replies = iter(({"owned_keys": [{"game_id": 1, "id": 12, "game": game}]},
                        {"owned_keys": [{"game_id": 1, "id": 13, "game": game}]},
                        {"owned_keys": {}}))
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=next(replies)))
        with ItchClient("test-key", transport) as client:
            self.assertEqual(list(client.owned_assets()), [Asset(1, "Pack", "creator", game["url"], (12, 13))])

    def test_sign_in_download_and_check_again(self):
        package = archive_bytes()
        file_requests = []

        def handler(request):
            path = request.url.path
            if path == "/profile":
                return httpx.Response(200, json={"user": {"id": 10, "username": "tester"}})
            if path == "/profile/owned-keys":
                keys = [{"game_id": 1, "id": 99, "game": {"title": "Pack"}}]
                return httpx.Response(200, json={"owned_keys": keys if request.url.params["page"] == "1" else []})
            if path == "/games/1/uploads":
                return httpx.Response(200, json={"uploads": [{
                    "id": 45, "filename": "asset.zip", "size": len(package), "storage": "hosted",
                }]})
            if path == "/uploads/45/download":
                return httpx.Response(200, json={"url": "https://cdn.example/asset.zip"})
            self.assertEqual(path, "/asset.zip")
            file_requests.append(path)
            return httpx.Response(200, stream=httpx.ByteStream(package))

        class Credentials:
            def set_password(self, service, account, key):
                self.saved = (service, account, key)

            def get_password(self, service, account):
                return self.saved[2] if self.saved[:2] == (service, account) else None

        with TemporaryDirectory() as temp:
            database = Path(temp) / "library.db"
            store = Store(database)
            credentials = Credentials()
            client_factory = lambda key: ItchClient(key, httpx.MockTransport(handler))
            updater = Updater(store, credentials, client_factory)
            self.assertEqual(updater.connect("test-key"), (10, 1))
            self.assertEqual(updater.check(10), CheckResult())
            store.set_setting("download_root", str(Path(temp) / "GameAssets"))
            store.track(10, 1, True)
            upload = next(iter(updater.check(10).pending[1]))
            downloaded = updater.download_many(10, [(1, upload)], lambda *_: None)
            self.assertEqual(downloaded.saved, 1)
            self.assertEqual(downloaded.pending, {1: {}})
            version = store.versions(10, 1)[0]
            self.assertEqual((version.path / "asset.txt").read_bytes(), b"asset")
            self.assertEqual([item.name for item in version.path.iterdir()], ["asset.txt"])
            self.assertEqual(version.path.parent.name, "Pack [1]")
            self.assertEqual(version.manifest.entries, {"asset.txt": 5})
            legacy_path = version.path.with_name("legacy-version") / "Extracted"
            legacy_path.parent.mkdir()
            version.path.rename(legacy_path)
            with store.connect() as connection:
                connection.execute("UPDATE versions SET path = ?, current = 0", (str(legacy_path),))
            restarted = Updater(Store(database), credentials, client_factory)
            self.assertEqual(restarted.check(10), CheckResult(pending={1: {}}))
            self.assertEqual(file_requests, ["/asset.zip"])
            self.assertEqual(store.library(10)[0].status, "Up to date")
            (legacy_path / "asset.txt").unlink()
            self.assertEqual(restarted.check(10), CheckResult(pending={1: {upload: "Files missing locally"}}))
            store.set_pack_settings(10, 1, None, KEEP_ARCHIVES)
            restored = restarted.download_many(10, [(1, upload)], lambda *_: None)
            self.assertEqual(restored.saved, 1)
            archived = store.versions(10, 1)[0]
            self.assertIsNone(archived.manifest)
            self.assertEqual(archived.path.read_bytes(), package)
            self.assertEqual(list(archived.path.parent.iterdir()), [archived.path])

    def test_hidden_files_persist_and_new_files_show(self):
        with TemporaryDirectory() as temp:
            db = Path(temp) / "library.db"
            store = Store(db)
            asset = Asset(1, "Legacy Collection", "Ansimuz", "", (99,))
            store.import_library(10, [asset])
            store.track(10, asset.id, True)
            coupon = Upload(1, "Coupon codes.txt", 3, "hosted", 99)
            archive = Upload(2, "Legacy Collection.zip", 4, "hosted", 99)
            store.save_uploads(10, asset.id, [coupon, archive])
            initial = Updater(store, object())
            self.assertEqual(set(initial.change_upload_visibility(10, asset.id, {coupon.id: False})), {archive})
            self.assertIsNone(store.library(10)[0].last_checked)

            new_file = Upload(3, "New tier.zip", 5, "hosted", 99)
            modified_coupon = Upload(1, "Coupon codes.txt", 6, "hosted", 99, updated_at="later")

            class Client:
                def uploads(self, _):
                    return [modified_coupon, archive, new_file]

            reopened = Store(db)
            updater = Updater(reopened, object())
            changes = updater._check_asset(Client(), 10, asset, [])
            self.assertEqual(set(changes), {archive, new_file})
            self.assertEqual(reopened.hidden_uploads(10, asset.id), {coupon.id})
            self.assertEqual(set(updater.change_upload_visibility(10, asset.id, {coupon.id: True})),
                             {modified_coupon, archive, new_file})

    def test_batch_uses_one_check_per_pack_and_continues_after_a_failed_file(self):
        package = archive_bytes()
        polled, transfers = [], []

        def handler(request):
            path = request.url.path
            if path.startswith("/games/"):
                asset = int(path.split("/")[2])
                polled.append(asset)
                ids = [11, 12, 13] if asset == 1 else [21]
                return httpx.Response(200, json={"uploads": [
                    {"id": i, "filename": f"file-{i}.zip", "size": len(package), "storage": "hosted"} for i in ids
                ]})
            if path.startswith("/uploads/"):
                upload = int(path.split("/")[2])
                transfers.append(upload)
                if upload == 12 and transfers.count(upload) == 1:
                    return httpx.Response(403)
                return httpx.Response(302, headers={"Location": f"https://cdn.example/{upload}.zip"})
            return httpx.Response(200, stream=httpx.ByteStream(package))

        class Credentials:
            def get_password(self, service, account):
                return "test-key"

        with TemporaryDirectory() as temp:
            store = Store(Path(temp) / "library.db")
            store.import_library(10, [Asset(i, f"Pack {i}", "Creator", "", (i,)) for i in (1, 2)])
            for asset in (1, 2):
                store.track(10, asset, True)
            store.set_setting("download_root", str(Path(temp) / "GameAssets"))
            updater = Updater(store, Credentials(), lambda key: ItchClient(key, httpx.MockTransport(handler)))
            pending = updater.check(10).pending
            selections = [(asset, upload) for asset, changes in pending.items() for upload in changes]
            store.set_upload_visibility(10, 1, {13: False})
            polled.clear()
            batch = updater.download_many(10, selections, lambda *_: None)
            self.assertEqual(polled, [1, 2])
            self.assertEqual(transfers, [11, 12, 21])
            self.assertEqual(batch.saved, 2)
            self.assertEqual(len(batch.errors), 2)
            self.assertEqual([upload.id for upload in batch.pending[1]], [12])
            self.assertFalse(batch.pending[2])
            self.assertEqual([(asset, upload.id) for asset, upload in batch.retryable], [(1, 12)])
            retry = updater.download_many(10, batch.retryable, lambda *_: None)
            self.assertEqual(retry.saved, 1)
            self.assertFalse(retry.errors)
            self.assertEqual(transfers, [11, 12, 21, 12])

    def test_upload_listing_tries_other_keys_after_access_denials(self):
        def handler(request):
            key = request.url.params["download_key_id"]
            checked.append(key)
            if key == "14":
                return httpx.Response(403)
            if key == "15":
                return httpx.Response(200, json={"errors": ["Invalid download key"]})
            return httpx.Response(200, json={"uploads": [
                {"id": 21, "filename": "asset.zip", "storage": "hosted"},
            ]})

        checked = []
        with ItchClient("test-key", httpx.MockTransport(handler)) as client:
            uploads = client.uploads(Asset(1, "Pack", "Creator", "", (14, 15, 16)))
            self.assertEqual([(upload.id, upload.key_id) for upload in uploads], [(21, 16)])
            self.assertEqual(checked, ["14", "15", "16"])
            with self.assertRaises(ItchError):
                client.uploads(Asset(1, "Pack", "Creator", "", (14, 15)))

    def test_pack_rename_keeps_existing_folder_including_migrated_downloads(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "library.db")
            store.set_setting("download_root", str(root / "GameAssets"))
            updater = Updater(store, object())
            store.import_library(1, [Asset(1, "Old title", "Old creator", "", (1,))])
            destination = updater.folder(store.library(1)[0])
            first, second = archive_bytes(), archive_bytes(contents=b"new version")
            initial, update = hosted_upload(first), hosted_upload(second, "later")
            saved = download_version(Transfer(first), initial, destination, [], lambda *_: None,
                                     record=DownloadRecord(store, 1, 1, initial))
            store.import_library(1, [Asset(1, "New title", "New creator", "", (1,))])
            self.assertEqual(updater.folder(store.library(1)[0]), destination)
            with store.connect() as connection:
                connection.execute("UPDATE assets SET default_folder = NULL")
            reopened = Store(store.database)
            updater = Updater(reopened, object())
            self.assertEqual(updater.folder(reopened.library(1)[0]), destination)
            newer = download_version(Transfer(second), update, updater.folder(reopened.library(1)[0]),
                                     reopened.versions(1, 1), lambda *_: None,
                                     record=DownloadRecord(reopened, 1, 1, update))
            self.assertEqual(newer.path, saved.path)
            self.assertEqual((newer.path / "asset.txt").read_bytes(), b"new version")


class DownloadTests(unittest.TestCase):
    def test_cdn_redirects_never_receive_the_api_key(self):
        def handler(request):
            if request.url.host == "api.itch.io":
                self.assertEqual(request.headers.get("Authorization"), "Bearer test-key")
                self.assertEqual(request.url.params.get("download_key_id"), "99")
                return httpx.Response(302, headers={"Location": "https://files.example/first"})
            self.assertNotIn("Authorization", request.headers)
            if request.url.path == "/first":
                return httpx.Response(302, headers={"Location": "/second"})
            return httpx.Response(200, stream=httpx.ByteStream(b"abc"))

        with ItchClient("test-key", httpx.MockTransport(handler)) as client:
            with client.open_download(hosted_upload(b"abc")) as response:
                self.assertEqual(b"".join(response.iter_raw()), b"abc")

    def test_current_folder_replacement_duplicates_and_interruption(self):
        with TemporaryDirectory() as temp:
            destination = Path(temp) / "downloads"
            first = archive_bytes(contents=b"version 1")
            second = archive_bytes(contents=b"version 2")
            version = download_version(Transfer(first), hosted_upload(first), destination, [], lambda *_: None)
            self.assertTrue(version.created)
            self.assertEqual(version.path.name, "asset")
            self.assertEqual((version.path / "asset.txt").read_bytes(), b"version 1")
            earlier = [version]
            duplicate = download_version(Transfer(first), hosted_upload(first, "newer"), destination, earlier,
                                         lambda *_: None)
            self.assertFalse(duplicate.created)
            self.assertEqual(duplicate.path, version.path)
            self.assertEqual(duplicate.digest, version.digest)
            self.assertEqual(duplicate.manifest, version.manifest)
            with self.assertRaises(ItchError):
                download_version(Transfer(second, interrupted=True), hosted_upload(second, "newer"),
                                 destination, earlier, lambda *_: None)
            self.assertEqual(list(destination.iterdir()), [version.path])
            newer = download_version(Transfer(second), hosted_upload(second, "newer"), destination,
                                     earlier, lambda *_: None)
            self.assertTrue(newer.created)
            self.assertTrue(newer.replaced)
            self.assertEqual(newer.path, version.path)
            self.assertEqual(list(destination.iterdir()), [newer.path])
            self.assertEqual((newer.path / "asset.txt").read_bytes(), b"version 2")

    def test_replacement_preserves_local_edits_and_other_uploads(self):
        with TemporaryDirectory() as temp:
            destination = Path(temp)
            first = archive_bytes(contents=b"version 1")
            second = archive_bytes(contents=b"version 2")
            saved = download_version(Transfer(first), hosted_upload(first), destination, [], lambda *_: None)
            (saved.path / "asset.txt").write_bytes(b"my edits!")
            other = Upload(56, "asset.zip", len(first), "hosted", 99)
            other_saved = download_version(Transfer(first), other, destination, [], lambda *_: None)
            self.assertEqual(other_saved.path.name, "asset (2)")
            newer = download_version(Transfer(second), hosted_upload(second, "newer"), destination,
                                     [saved], lambda *_: None)
            self.assertTrue(newer.preserved)
            self.assertEqual(newer.path.name, "asset (3)")
            self.assertEqual((saved.path / "asset.txt").read_bytes(), b"my edits!")
            self.assertEqual((other_saved.path / "asset.txt").read_bytes(), b"version 1")

    def test_record_failure_rolls_back_and_retry_replaces_current(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "library.db")
            destination = root / "downloads"
            first = archive_bytes(contents=b"version 1")
            second = archive_bytes(contents=b"version 2")
            initial, update = hosted_upload(first), hosted_upload(second, "newer")
            saved = download_version(Transfer(first), initial, destination, [], lambda *_: None,
                                     record=DownloadRecord(store, 1, 1, initial))

            class FailingRecord(DownloadRecord):
                def save(self, download):
                    raise OSError("Could not record request")

            with self.assertRaises(OSError):
                download_version(Transfer(second), update, destination, [saved], lambda *_: None,
                                 record=FailingRecord(store, 1, 1, update))
            self.assertEqual((saved.path / "asset.txt").read_bytes(), b"version 1")
            self.assertEqual(list(destination.iterdir()), [saved.path])
            result = download_version(Transfer(second), update, destination, [saved], lambda *_: None,
                                      record=DownloadRecord(store, 1, 1, update))
            self.assertEqual(saved.path, result.path)
            self.assertEqual([version.revision for version in store.versions(1, 1)], [update.revision])
            self.assertEqual(list(destination.iterdir()), [saved.path])

    def test_recovery_restores_backup_after_uncommitted_swap(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "library.db")
            destination = root / "downloads"
            first = archive_bytes(contents=b"version 1")
            initial = hosted_upload(first)
            saved = download_version(Transfer(first), initial, destination, [], lambda *_: None,
                                     record=DownloadRecord(store, 1, 1, initial))
            backup = destination / ".quick-itch-backup-test"
            record = DownloadRecord(store, 1, 1, hosted_upload(first, "newer"))
            record.begin(saved.path, backup, saved)
            saved.path.rename(backup)
            saved.path.mkdir()
            (saved.path / "asset.txt").write_bytes(b"version 2")
            Store(store.database).recover_downloads(1, 1)
            self.assertEqual((saved.path / "asset.txt").read_bytes(), b"version 1")
            self.assertFalse(backup.exists())
            self.assertEqual(store.versions(1, 1)[0].revision, initial.revision)

    def test_replacement_keeps_edits_saved_during_the_swap(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first, second = archive_bytes(), archive_bytes(contents=b"new version")
            saved = download_version(Transfer(first), hosted_upload(first), root, [], lambda *_: None)
            original_rename = Path.rename

            def save_local_edit(path, target):
                if path == saved.path and target.name.startswith(".quick-itch-backup-"):
                    (path / "notes.txt").write_text("Local edits")
                return original_rename(path, target)

            with patch.object(Path, "rename", save_local_edit):
                result = download_version(Transfer(second), hosted_upload(second, "later"), root, [saved], lambda *_: None)
            self.assertEqual((result.path / "asset.txt").read_bytes(), b"new version")
            self.assertEqual((result.retained_backup / "notes.txt").read_text(), "Local edits")

    def test_committed_leftover_backup_does_not_block_checks_or_new_updates(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "library.db")
            asset = Asset(1, "Pack", "Creator", "", (1,))
            store.import_library(1, [asset])
            store.track(1, 1, True)
            destination = root / "downloads"
            first, second, third = (archive_bytes(contents=content) for content in (b"first", b"second", b"third"))
            initial, update, next_update = hosted_upload(first), hosted_upload(second, "later"), hosted_upload(third, "latest")
            download_version(Transfer(first), initial, destination, [], lambda *_: None,
                             record=DownloadRecord(store, 1, 1, initial))
            with patch("quick_itch.files.finish_backup", side_effect=PermissionError):
                saved = download_version(Transfer(second), update, destination, store.versions(1, 1),
                                         lambda *_: None, record=DownloadRecord(store, 1, 1, update))
            self.assertTrue(saved.cleanup_pending)
            notes = saved.path / "notes.txt"
            notes.write_text("Local edits")

            class Client:
                def uploads(self, _):
                    return [next_update]

            retained = []
            changes = Updater(store, object())._check_asset(Client(), 1, asset, retained)
            self.assertIn(next_update, changes)
            self.assertEqual(len(retained), 1)
            self.assertEqual((retained[0] / "asset.txt").read_bytes(), b"first")
            self.assertEqual(store.recover_downloads(1, 1), [])
            newer = download_version(Transfer(third), next_update, destination, store.versions(1, 1),
                                     lambda *_: None, record=DownloadRecord(store, 1, 1, next_update))
            self.assertEqual((newer.path / "asset.txt").read_bytes(), b"third")
            self.assertEqual(notes.read_text(), "Local edits")

    def test_corrupt_deflate_is_reported_as_a_file_failure(self):
        package = bytearray(archive_bytes(compression=ZIP_DEFLATED))
        package[30 + len("asset.txt")] = 0xff
        with TemporaryDirectory() as temp:
            destination = Path(temp) / "downloads"
            with self.assertRaisesRegex(FileSafetyError, "damaged"):
                download_version(Transfer(package), hosted_upload(package), destination, [], lambda *_: None)
            self.assertEqual(list(destination.iterdir()), [])

    def test_long_zip_filename_still_extracts(self):
        package = archive_bytes()
        upload = Upload(10, "Long filename " * 8 + ".ZIP", len(package), "hosted", 99)
        with TemporaryDirectory() as temp:
            saved = download_version(Transfer(package), upload, Path(temp), [], lambda *_: None)
            self.assertEqual((saved.path / "asset.txt").read_bytes(), b"asset")
            self.assertTrue(saved.manifest.filename.endswith(".ZIP"))

    def test_unsupported_zip_keeps_only_the_original(self):
        data = bytearray(archive_bytes())
        for marker, offset in ((b"PK\x03\x04", 8), (b"PK\x01\x02", 10)):
            start = data.index(marker) + offset
            data[start:start + 2] = (99).to_bytes(2, "little")
        with TemporaryDirectory() as temp:
            version = download_version(Transfer(data), hosted_upload(data), Path(temp), [], lambda *_: None)
            self.assertIsNone(version.manifest)
            self.assertEqual(version.path.read_bytes(), data)
            self.assertEqual(list(version.path.parent.iterdir()), [version.path])

    def test_rejects_archive_traversal_and_windows_paths(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for bad_name in ("../escape.txt", "/root.txt", "C:/drive.txt", "pack\\escape.txt", "NUL.txt", "file:ads"):
                with self.subTest(bad_name=bad_name):
                    normalized = bad_name.replace("\\", "/")
                    data = archive_bytes(normalized).replace(normalized.encode(), bad_name.encode())
                    (root / "test.zip").write_bytes(data)
                    with self.assertRaises(FileSafetyError):
                        extract_zip(root / "test.zip", root / "extracted")
            link = ZipInfo("link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            (root / "test.zip").write_bytes(archive_bytes(link))
            with self.assertRaises(FileSafetyError):
                extract_zip(root / "test.zip", root / "extracted")
            self.assertFalse((root / "extracted").exists())

    def test_rejects_unity_project_destinations(self):
        with TemporaryDirectory() as temp:
            project = Path(temp) / "My Game"
            settings = project / "ProjectSettings"
            settings.mkdir(parents=True)
            (settings / "ProjectVersion.txt").touch()
            with self.assertRaisesRegex(FileSafetyError, "Unity project"):
                validate_destination(project / "Assets" / "Downloads")
