"""Read the Photos app's database (Photos.sqlite) from a backup, and use it to explain leftover thumbnails.

The database has one row per photo or video in the library, in a table called ZASSET (ZGENERICASSET before
iOS 14). A row holds the file's folder and name under Media/, when it was taken, where, and whether it's in
Recently Deleted. When a photo is deleted for good, its row goes too. So for a thumbnail whose original
isn't in the backup:

- no row: the photo is no longer in the library, and the thumbnail is what's left of it;
- a row marked as trashed: the photo is in Recently Deleted;
- any other row: the photo is still in the library, and its original just isn't in this backup (usually
  because iCloud Photos keeps it in iCloud and the phone held only a small copy).

The database is copied out of the backup before it's opened, with its write-ahead log (Photos.sqlite-wal)
beside it, so SQLite applies the changes still waiting in the log. Nothing in the backup is touched.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .backup import Backup
from .camera_roll import DATABASE, CameraRoll, Thumbnail

CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)  # Apple stores dates as seconds since this
TABLES = ("ZASSET", "ZGENERICASSET")  # iOS 14 and later; iOS 13 and earlier
FIELDS = (
    "ZKIND",
    "ZDATECREATED",
    "ZLATITUDE",
    "ZLONGITUDE",
    "ZTRASHEDSTATE",
    "ZTRASHEDDATE",
    "ZHIDDEN",
)
KINDS = {0: "photo", 1: "video"}

# What became of a photo whose thumbnail is in the backup but whose original isn't.
GONE = "gone"
RECENTLY_DELETED = "recently deleted"
IN_LIBRARY = "in library"
UNKNOWN = "unknown"
STATUSES = (GONE, RECENTLY_DELETED, IN_LIBRARY, UNKNOWN)
LABELS = {
    GONE: "No longer in the library: only thumbnails are left",
    RECENTLY_DELETED: "In Recently Deleted",
    IN_LIBRARY: "Still in the library, original not in this backup",
    UNKNOWN: "Deleted, or kept in iCloud",
}
HEADINGS = {
    **LABELS,
    IN_LIBRARY: "Still in the library; the original isn't in this backup, usually because it's kept in iCloud",
}


class LibraryError(Exception):
    """The Photos database is missing or can't be read; the message says which, in plain words."""


@dataclass(frozen=True)
class Asset:
    """One photo or video in the Photos library."""

    path: str  # where its original belongs in the backup, e.g. Media/DCIM/100APPLE/IMG_0001.HEIC
    kind: str | None = None  # "photo" or "video"
    taken: datetime | None = None
    place: tuple[float, float] | None = None  # latitude, longitude
    trashed: bool = False  # in Recently Deleted
    trashed_on: datetime | None = None
    hidden: bool = False
    original_name: str | None = None  # the name it was imported under, if different


@dataclass(frozen=True)
class Library:
    table: str
    assets: dict[str, Asset]  # by path

    def count(self, **flags: bool) -> int:
        return sum(1 for asset in self.assets.values() if all(getattr(asset, k) == v for k, v in flags.items()))


@dataclass(frozen=True)
class Leftover:
    """A photo or video whose thumbnails are in the backup but whose original isn't."""

    original: str
    thumbnails: tuple[Thumbnail, ...]
    status: str
    asset: Asset | None = None

    @property
    def thumbnail_saved(self) -> datetime | None:
        """When the first of its thumbnails was saved: close to when the photo reached the phone."""
        times = [t.file.modified for t in self.thumbnails if t.file.modified is not None]
        return min(times) if times else None


def read_library(backup: Backup, roll: CameraRoll) -> Library:
    """Read the Photos library's database from a backup (a copy of it, never the backup itself)."""
    database = roll.database_file(DATABASE[0])
    if database is None:
        raise LibraryError("The Photos database (Photos.sqlite) isn't in this backup.")
    source = backup.locate(database)
    if source is None:
        raise LibraryError("The Photos database is listed in this backup, but its file is missing.")
    log = roll.database_file(DATABASE[1])
    log_source = backup.locate(log) if log is not None else None

    with tempfile.TemporaryDirectory(prefix="afterimage-") as folder:
        copy = Path(folder) / "Photos.sqlite"
        shutil.copyfile(source, copy)
        if log_source is not None:
            shutil.copyfile(log_source, Path(folder) / "Photos.sqlite-wal")
        db = sqlite3.connect(copy)
        try:
            return _read(db)
        except sqlite3.DatabaseError as error:
            raise LibraryError(f"The Photos database couldn't be read: {error}.") from error
        finally:
            db.close()


def _read(db: sqlite3.Connection) -> Library:
    tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    table = next((t for t in TABLES if t in tables), None)
    if table is None:
        raise LibraryError("The Photos database has no table of photos (ZASSET or ZGENERICASSET).")
    columns = _columns(db, table)
    if not {"ZDIRECTORY", "ZFILENAME"} <= columns:
        raise LibraryError(f"The Photos database's {table} table has no file names.")

    select = ["a.ZDIRECTORY", "a.ZFILENAME", *(f"a.{c}" if c in columns else "NULL" for c in FIELDS)]
    join = ""
    if (
        "ZADDITIONALATTRIBUTES" in columns
        and "ZADDITIONALASSETATTRIBUTES" in tables
        and "ZORIGINALFILENAME" in _columns(db, "ZADDITIONALASSETATTRIBUTES")
    ):
        select.append("x.ZORIGINALFILENAME")
        join = " LEFT JOIN ZADDITIONALASSETATTRIBUTES x ON x.Z_PK = a.ZADDITIONALATTRIBUTES"
    else:
        select.append("NULL")

    assets: dict[str, Asset] = {}
    for row in db.execute(f"SELECT {', '.join(select)} FROM {table} a{join}"):
        asset = _to_asset(row)
        if asset is not None:
            assets[asset.path] = asset
    return Library(table, assets)


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _to_asset(row: tuple[Any, ...]) -> Asset | None:
    directory, filename, kind, taken, latitude, longitude, trashed, trashed_on, hidden, original = row
    if not isinstance(filename, str) or not filename:
        return None
    folder = directory.strip("/") if isinstance(directory, str) else ""
    path = f"Media/{folder}/{filename}" if folder else f"Media/{filename}"
    return Asset(
        path=path,
        kind=KINDS.get(kind),
        taken=_core_data_date(taken),
        place=_place(latitude, longitude),
        trashed=trashed == 1,
        trashed_on=_core_data_date(trashed_on),
        hidden=hidden == 1,
        original_name=original if isinstance(original, str) and original and original != filename else None,
    )


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _core_data_date(value: Any) -> datetime | None:
    seconds = _number(value)
    if seconds is None:
        return None
    try:
        return CORE_DATA_EPOCH + timedelta(seconds=seconds)
    except (OverflowError, ValueError):
        return None


def _place(latitude: Any, longitude: Any) -> tuple[float, float] | None:
    """A photo's coordinates, or None. Photos without a location are stored as -180, -180."""
    lat, lon = _number(latitude), _number(longitude)
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat, lon) == (0, 0):
        return None
    return lat, lon


def find_leftovers(roll: CameraRoll, library: Library | None) -> list[Leftover]:
    """Every photo or video with thumbnails but no original in the backup, and what became of it.

    Without a library (no readable Photos database), each one's status is UNKNOWN.
    """
    leftovers = []
    for original in roll.without_original():
        thumbnails = tuple(roll.thumbnails[original])
        if library is None:
            leftovers.append(Leftover(original, thumbnails, UNKNOWN))
            continue
        asset = library.assets.get(original)
        status = GONE if asset is None else RECENTLY_DELETED if asset.trashed else IN_LIBRARY
        leftovers.append(Leftover(original, thumbnails, status, asset))
    leftovers.sort(key=lambda leftover: (STATUSES.index(leftover.status), leftover.original))
    return leftovers


def recently_deleted_in_backup(roll: CameraRoll, library: Library | None) -> list[Asset]:
    """Photos and videos in Recently Deleted whose full-size original is still in the backup."""
    if library is None:
        return []
    return sorted((a for a in library.assets.values() if a.trashed and a.path in roll.by_path), key=lambda a: a.path)


def by_status(leftovers: Iterable[Leftover]) -> dict[str, list[Leftover]]:
    groups: dict[str, list[Leftover]] = {}
    for leftover in leftovers:
        groups.setdefault(leftover.status, []).append(leftover)
    return groups
