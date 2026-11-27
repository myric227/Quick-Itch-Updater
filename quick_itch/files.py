from dataclasses import dataclass, field, replace
import hashlib
from itertools import count
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from tempfile import TemporaryDirectory
from uuid import uuid4
from zipfile import BadZipFile, ZipFile
import zlib


class FileSafetyError(Exception):
    pass


class ArchiveNeedsOriginal(Exception):
    pass


EXTRACT_ZIPS = "extract"
KEEP_ARCHIVES = "archive"
WINDOWS_RESERVED = re.compile(r"(?:CON|CONIN\$|CONOUT\$|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])(?:\.|$)", re.I)
WINDOWS_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def file_component(value, *, keep_suffix=False):
    name = WINDOWS_INVALID.sub("_", value).strip().rstrip(". ")
    suffix = PurePosixPath(name).suffix if keep_suffix else ""
    if len(name) > 100 and 0 < len(suffix) < 100:
        name = name[:100 - len(suffix)].rstrip(". ") + suffix
    else:
        name = name[:100].rstrip(". ") or "Untitled"
    return f"_{name}" if WINDOWS_RESERVED.match(name) else name


def validate_destination(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise FileSafetyError("Choose an absolute download folder outside your Unity projects.")
    for parent in (*reversed(path.parents), path):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if parent.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise FileSafetyError("Choose a download folder without symlinks or junctions.")
        if not parent.is_dir():
            raise FileSafetyError("The download location includes a file where a folder is needed.")
        if (parent / "ProjectSettings" / "ProjectVersion.txt").is_file() or (
            (parent / "Assets").is_dir() and (parent / "ProjectSettings").is_dir()
        ):
            raise FileSafetyError("Choose a download folder outside the Unity project.")
    return path


@dataclass(frozen=True)
class ExtractedManifest:
    filename: str
    entries: dict[str, int | None]
    hashes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SavedDownload:
    path: Path
    digest: str
    manifest: ExtractedManifest | None
    created: bool = True
    current: bool = False
    replaced: bool = False
    preserved: bool = False
    cleanup_pending: bool = False
    retained_backup: Path | None = None


def matches_download(root, version):
    """Check that a managed folder contains only the unedited downloaded files."""
    try:
        validate_destination(root)
        if version.manifest is None:
            expected = {version.path.name.casefold(): version.digest}
            directories = set()
        else:
            expected = {name.casefold(): digest for name, digest in version.manifest.hashes.items()}
            directories = set()
            for name, size in version.manifest.entries.items():
                path = PurePosixPath(name)
                if size is None:
                    directories.add(name.casefold())
                elif name.casefold() not in expected:
                    return False
                directories.update(parent.as_posix().casefold() for parent in path.parents if parent.parts)
        folders = [root]
        while folders:
            folder = folders.pop()
            for item in folder.iterdir():
                info = item.lstat()
                if item.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    return False
                name = item.relative_to(root).as_posix().casefold()
                if stat.S_ISDIR(info.st_mode) and name in directories:
                    directories.remove(name)
                    folders.append(item)
                elif stat.S_ISREG(info.st_mode) and name in expected:
                    with item.open("rb") as file:
                        if hashlib.file_digest(file, "sha256").hexdigest() != expected.pop(name):
                            return False
                else:
                    return False
        return not (expected or directories)
    except (FileSafetyError, OSError):
        return False


def validate_backup(root, backup):
    validate_destination(backup)
    parent = root.parent.resolve(strict=True)
    if backup.resolve(strict=True).parent != parent or not backup.name.startswith(".quick-itch-backup-"):
        raise FileSafetyError("Could not verify the temporary backup folder.")


def finish_backup(root, backup, previous):
    if not os.path.lexists(backup):
        return None
    validate_backup(root, backup)
    if previous is not None and matches_download(backup, previous):
        shutil.rmtree(backup)
        return None
    label = file_component(root.name)
    for number in count(1):
        suffix = "previous copy" if number == 1 else f"previous copy {number}"
        preserved = root.with_name(f"{label} ({suffix})")
        if os.path.lexists(preserved):
            continue
        try:
            return backup.rename(preserved)
        except FileExistsError:
            continue


def recover_replacement(root, backup):
    if not os.path.lexists(backup):
        return
    validate_backup(root, backup)
    if os.path.lexists(root):
        validate_destination(root)
        root.rename(root.with_name(f".quick-itch-recovered-{uuid4().hex}"))
    backup.rename(root)


def version_available(path, manifest):
    try:
        if manifest is None:
            return path.is_file()
        if not path.is_dir():
            return False
        for relative, size in manifest.entries.items():
            info = path.joinpath(*PurePosixPath(relative).parts).stat()
            if size is None:
                if not stat.S_ISDIR(info.st_mode):
                    return False
            elif not stat.S_ISREG(info.st_mode) or info.st_size != size:
                return False
        return True
    except OSError:
        return False


def extract_zip(archive_path, destination):
    try:
        with ZipFile(archive_path) as archive:
            entries = []
            paths = {}
            explicit_paths = set()
            for member in archive.infolist():
                raw_parts = member.orig_filename.rstrip("/").split("/")
                parts = tuple(part.rstrip(" .") for part in raw_parts)
                mode = stat.S_IFMT(member.external_attr >> 16)
                if any(
                    not part or WINDOWS_RESERVED.match(part) or WINDOWS_INVALID.search(part)
                    for part in parts
                ) or mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise FileSafetyError("The ZIP contains an unsafe path or a special file.")
                folded = tuple(part.casefold() for part in parts)
                if folded in explicit_paths:
                    raise FileSafetyError("The ZIP contains duplicate paths on Windows.")
                explicit_paths.add(folded)
                original = tuple(part.casefold() for part in raw_parts)
                for depth in range(1, len(parts) + 1):
                    # Check implicit directories too so normalization cannot merge two folders.
                    source = (original[:depth], depth < len(parts) or member.is_dir())
                    if paths.setdefault(folded[:depth], source) != source:
                        raise FileSafetyError("The ZIP contains conflicting paths on Windows.")
                entries.append((member, PurePosixPath(*parts)))
            if any(member.flag_bits & 1 for member, _ in entries):
                raise ArchiveNeedsOriginal("This ZIP is password-protected.")
            if sum(member.file_size for member, _ in entries) > shutil.disk_usage(destination.parent).free:
                raise FileSafetyError("There is insufficient space to extract this ZIP.")
            destination.mkdir()
            hashes = {}
            for member, relative in entries:
                output = destination.joinpath(*relative.parts)
                if member.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                else:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    with archive.open(member) as source, output.open("xb") as file:
                        while chunk := source.read(256 * 1024):
                            file.write(chunk)
                            digest.update(chunk)
                    hashes[relative.as_posix()] = digest.hexdigest()
            return ExtractedManifest(archive_path.name, {relative.as_posix(): None if member.is_dir() else member.file_size
                                                         for member, relative in entries}, hashes)
    except NotImplementedError as error:
        raise ArchiveNeedsOriginal("This ZIP uses unsupported compression.") from error
    except (BadZipFile, RuntimeError, zlib.error, lzma.LZMAError, EOFError) as error:
        raise FileSafetyError("The ZIP is damaged and could not be extracted.") from error


def replace_current(staging, root, download, previous, record):
    backup = root.with_name(f".quick-itch-backup-{uuid4().hex}")
    if record:
        record.begin(root, backup, previous)
    published = False
    try:
        root.rename(backup)
        staging.rename(root)
        published = True
        if record:
            record.save(download)
    except BaseException:
        if published:
            root.rename(staging)
        if os.path.lexists(backup):
            backup.rename(root)
        if record:
            record.finish()
        raise
    try:
        retained = finish_backup(root, backup, previous)
    except (FileSafetyError, OSError):
        return replace(download, cleanup_pending=True)
    if record:
        record.finish()
    return replace(download, retained_backup=retained) if retained else download


def download_version(client, upload, destination, earlier, progress, mode=EXTRACT_ZIPS, *, record=None):
    destination = validate_destination(destination)
    destination.mkdir(parents=True, exist_ok=True)
    filename = file_component(upload.filename, keep_suffix=True)
    with TemporaryDirectory(prefix=".quick-itch-", dir=destination) as temporary:
        working = Path(temporary)
        transfer = working / "transfer"
        transfer.mkdir()
        package = transfer / filename
        digest = hashlib.sha256()
        received = 0
        with client.open_download(upload) as response, package.open("xb") as file:
            expected = upload.size if upload.storage == "hosted" and upload.size else None
            if expected is None:
                value = response.headers.get("Content-Length", "")
                expected = int(value) if value.isdecimal() else None
            for chunk in response.iter_raw(256 * 1024):
                received += len(chunk)
                if expected is not None and received > expected:
                    raise FileSafetyError("The file exceeded the expected size. Check for updates and retry.")
                file.write(chunk)
                digest.update(chunk)
                progress(received, expected)
        if not received or (expected is not None and received != expected):
            raise FileSafetyError("The file was incomplete. You can retry the download.")
        checksum = digest.hexdigest()
        current = next((entry for entry in earlier if entry.current), None)
        candidates = [current] if current is not None else earlier
        for entry in candidates:
            if entry.digest != checksum or not version_available(entry.path, entry.manifest):
                continue
            if entry.manifest is None:
                with entry.path.open("rb") as file:
                    if hashlib.file_digest(file, "sha256").hexdigest() != checksum:
                        continue
            download = SavedDownload(entry.path, checksum, entry.manifest, created=False, current=entry.current)
            if record:
                record.save(download)
            return download
        manifest = None
        contents = working / "extraction"
        if mode == EXTRACT_ZIPS and package.suffix.lower() == ".zip":
            try:
                manifest = extract_zip(package, contents)
            except ArchiveNeedsOriginal:
                pass
        if manifest is not None:
            package.unlink()
        label = file_component(Path(filename).stem)
        validate_destination(destination)
        staging = contents if manifest is not None else transfer
        previous = current.path if current and current.manifest is not None else current.path.parent if current else None
        preserved = False
        available_path = None
        if previous is not None and previous.parent == destination:
            if not os.path.lexists(previous):
                available_path = previous
            elif matches_download(previous, current):
                path = previous if manifest is not None else previous / filename
                download = SavedDownload(path, checksum, manifest, current=True, replaced=True)
                return replace_current(staging, previous, download, current, record)
            else:
                preserved = True
        for number in count(1):
            name = label if number == 1 else f"{label} ({number})"
            published = available_path or destination / name
            available_path = None
            if os.path.lexists(published):
                continue
            try:
                staging.rename(published)
            except FileExistsError:
                continue
            path = published if manifest is not None else published / filename
            download = SavedDownload(path, checksum, manifest, current=True,
                                     replaced=published == previous, preserved=preserved)
            if record:
                try:
                    record.save(download)
                except BaseException:
                    published.rename(staging)
                    raise
            return download
