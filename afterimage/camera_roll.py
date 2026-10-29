"""Sort the camera roll part of a backup into originals, thumbnails and the rest.

The Photos app keeps small previews of every photo and video so the library scrolls quickly. In a backup
they're in CameraRollDomain under Media/PhotoData/Thumbnails/V2/, in a folder named after the original's
path, e.g.

    Media/PhotoData/Thumbnails/V2/DCIM/100APPLE/IMG_0001.HEIC/5005.JPG
    -> a preview of Media/DCIM/100APPLE/IMG_0001.HEIC

A preview whose original isn't in the backup is the interesting case. Either the photo was deleted and the
preview stayed behind, or the photo lives in iCloud and the phone only kept a small copy. The Photos
database tells which (see library.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .backup import Backup, BackupFile

DOMAIN = "CameraRollDomain"
THUMBNAILS = "Media/PhotoData/Thumbnails/"
THUMBNAILS_V2 = THUMBNAILS + "V2/"
ORIGINAL_FOLDERS = ("Media/DCIM/", "Media/PhotoData/CPLAssets/")  # the camera's own, and iCloud Photos'
EDITS = "Media/PhotoData/Mutations/"
DATABASE = ("Media/PhotoData/Photos.sqlite", "Media/PhotoData/Photos.sqlite-wal", "Media/PhotoData/Photos.sqlite-shm")
PHOTO_TYPES = frozenset({"jpg", "jpeg", "heic", "heif", "png", "gif", "dng", "tif", "tiff", "webp", "bmp"})
VIDEO_TYPES = frozenset({"mov", "mp4", "m4v", "avi", "3gp"})


def media_kind(path: str) -> str | None:
    """Say whether a path is a "photo" or a "video" from its extension, or None if it's neither."""
    name = path.rpartition("/")[2]
    stem, dot, extension = name.rpartition(".")
    if not (dot and stem):
        return None
    extension = extension.lower()
    if extension in PHOTO_TYPES:
        return "photo"
    if extension in VIDEO_TYPES:
        return "video"
    return None


def parse_thumbnail(relative_path: str) -> tuple[str, str] | None:
    """For a preview in Thumbnails/V2, the original's path and the preview's own name (e.g. "5005")."""
    if not relative_path.startswith(THUMBNAILS_V2):
        return None
    folder, slash, name = relative_path[len(THUMBNAILS_V2) :].rpartition("/")
    if not (slash and folder and name):
        return None
    stem, dot, _ = name.rpartition(".")
    return "Media/" + folder, stem if dot and stem else name


@dataclass(frozen=True)
class Thumbnail:
    file: BackupFile
    original: str  # the original's path in the backup, e.g. Media/DCIM/100APPLE/IMG_0001.HEIC
    variant: str  # the preview's own name without its extension, e.g. 5005


@dataclass
class CameraRoll:
    """The camera roll files in a backup, sorted by what they are."""

    originals: list[BackupFile] = field(default_factory=list)
    thumbnails: dict[str, list[Thumbnail]] = field(default_factory=dict)  # original's path -> its previews
    thumbnail_tables: list[BackupFile] = field(default_factory=list)  # .ithmb files: many previews in one
    edits: list[BackupFile] = field(default_factory=list)
    database: list[BackupFile] = field(default_factory=list)
    other: list[BackupFile] = field(default_factory=list)
    not_on_disk: list[BackupFile] = field(default_factory=list)  # in Manifest.db, missing from the folder
    by_path: dict[str, BackupFile] = field(default_factory=dict)  # every camera roll file, whatever it is

    @property
    def total(self) -> int:
        return (
            len(self.originals)
            + sum(len(previews) for previews in self.thumbnails.values())
            + len(self.thumbnail_tables)
            + len(self.edits)
            + len(self.database)
            + len(self.other)
        )

    def count(self, kind: str) -> int:
        return sum(1 for f in self.originals if media_kind(f.relative_path) == kind)

    @property
    def originals_size(self) -> int:
        return sum(f.size or 0 for f in self.originals)

    @property
    def has_database(self) -> bool:
        return self.database_file(DATABASE[0]) is not None

    def database_file(self, path: str) -> BackupFile | None:
        """Photos.sqlite, or its -wal or -shm file, if the backup lists it."""
        return next((f for f in self.database if f.relative_path == path), None)

    def without_original(self) -> list[str]:
        """Originals that have previews in the backup but aren't in it themselves."""
        return sorted(original for original in self.thumbnails if original not in self.by_path)


def scan(backup: Backup) -> CameraRoll:
    roll = CameraRoll()
    for entry in backup.files(DOMAIN):
        if entry.kind != "file":
            continue
        if backup.locate(entry) is None:
            roll.not_on_disk.append(entry)
        path = entry.relative_path
        roll.by_path[path] = entry
        if (parsed := parse_thumbnail(path)) is not None:
            original, variant = parsed
            roll.thumbnails.setdefault(original, []).append(Thumbnail(entry, original, variant))
        elif path.startswith(THUMBNAILS) and path.lower().endswith(".ithmb"):
            roll.thumbnail_tables.append(entry)
        elif path in DATABASE:
            roll.database.append(entry)
        elif path.startswith(EDITS):
            roll.edits.append(entry)
        elif path.startswith(ORIGINAL_FOLDERS) and media_kind(path):
            roll.originals.append(entry)
        else:
            roll.other.append(entry)
    return roll
