from dataclasses import dataclass
import json


@dataclass(frozen=True)
class Asset:
    id: int
    title: str
    creator: str
    url: str
    key_ids: tuple[int, ...]
    classification: str | None = None


@dataclass(frozen=True)
class Upload:
    id: int
    filename: str
    size: int | None
    storage: str
    key_id: int
    build_id: int | None = None
    updated_at: str | None = None
    release_at: str | None = None
    display_name: str | None = None

    @property
    def label(self):
        if self.display_name and self.display_name != self.filename:
            return f"{self.display_name} ({self.filename})"
        return self.filename

    @property
    def revision(self):
        if self.build_id:
            return f"build:{self.id}:{self.build_id}"
        return json.dumps([self.id, self.size, self.updated_at], separators=(",", ":"))


def pending_changes(uploads, saved, missing=()):
    """Compare the remote set with successfully saved or verified revisions."""
    changes = {}
    for upload in uploads:
        revisions = saved.get(upload.id, {})
        if upload.revision in revisions:
            continue
        if (upload.id, upload.revision) in missing:
            changes[upload] = "Files missing locally"
        elif not revisions:
            changes[upload] = "New file"
        elif upload.build_id or all(size != upload.size for size in revisions.values()):
            changes[upload] = "Update available"
        else:
            changes[upload] = "Possible change"
    return changes
