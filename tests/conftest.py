"""Fake iPhone backups, built in code, so the tests never need a real phone or real photos."""

from __future__ import annotations

import hashlib
import plistlib
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

WHEN = datetime(2026, 10, 20, 21, 14, tzinfo=timezone.utc)
UDID = "00008110-001A2B3C4D5E801E"
CAMERA_ROLL = "CameraRollDomain"
# The smallest bytes that start and end like a JPEG. Nothing in these tests decodes images.
JPEG = bytes.fromhex("ffd8ffe000104a464946000101") + bytes(20) + bytes.fromhex("ffd9")
HEIC = bytes.fromhex("0000001866747970686569630000000068656963") + bytes(1000)
MOV = bytes.fromhex("00000014667479707174202000000000") + bytes(3000)


def mbfile(path: str, size: int, modified: int, mode: int = 0o100644) -> bytes:
    """The NSKeyedArchiver record Manifest.db keeps for each file, in the shape Apple's tools write."""
    uid = plistlib.UID
    objects = [
        "$null",
        {
            "$class": uid(3),
            "RelativePath": uid(2),
            "Size": size,
            "LastModified": modified,
            "LastStatusChange": modified,
            "Birth": modified,
            "Mode": mode,
            "UserID": 501,
            "GroupID": 501,
            "InodeNumber": 1_234_567,
            "ProtectionClass": 3,
            "Flags": 0,
        },
        path,
        {"$classname": "MBFile", "$classes": ["MBFile", "NSObject"]},
    ]
    archive = {"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": uid(1)}, "$objects": objects}
    return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)


def _naive_utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass
class _Entry:
    domain: str
    path: str
    data: bytes
    flags: int
    on_disk: bool
    blob: bytes | None
    file_id: str | None


class FakeBackup:
    """Builds a backup folder shaped like the ones Finder and iTunes make (iOS 10 and later)."""

    def __init__(
        self,
        parent: Path,
        udid: str = UDID,
        device: str | None = "Om's iPhone",
        model: str = "iPhone15,2",
        ios: str = "17.5.1",
        when: datetime = WHEN,
    ) -> None:
        self.parent, self.udid, self.device, self.model, self.ios, self.when = parent, udid, device, model, ios, when
        self.encrypted = False
        self.status: str | None = "finished"  # None: no Status.plist at all
        self.manifest_db = True
        self.info_plist = True
        self.entries: list[_Entry] = []

    def file(
        self,
        domain: str,
        path: str,
        data: bytes = b"",
        *,
        on_disk: bool = True,
        blob: bytes | None = None,
        file_id: str | None = None,
    ) -> FakeBackup:
        self.entries.append(_Entry(domain, path, data, 1, on_disk, blob, file_id))
        return self

    def folder(self, domain: str, path: str) -> FakeBackup:
        self.entries.append(_Entry(domain, path, b"", 2, False, None, None))
        return self

    def build(self) -> Path:
        root = self.parent / self.udid
        root.mkdir(parents=True)
        when = _naive_utc(self.when)
        lockdown = {
            "DeviceName": self.device,
            "ProductType": self.model,
            "ProductVersion": self.ios,
            "UniqueDeviceID": self.udid,
            "SerialNumber": "SERIAL-DO-NOT-PRINT",
        }
        if self.device is None:
            del lockdown["DeviceName"]
        if self.info_plist:
            info = {
                "Product Type": self.model,
                "Product Version": self.ios,
                "Last Backup Date": when,
                "Unique Identifier": self.udid,
                "Serial Number": "SERIAL-DO-NOT-PRINT",
                "IMEI": "IMEI-DO-NOT-PRINT",
                "Phone Number": "PHONE-DO-NOT-PRINT",
            }
            if self.device is not None:
                info["Device Name"] = info["Display Name"] = self.device
            (root / "Info.plist").write_bytes(plistlib.dumps(info))  # Apple writes this one as XML
        manifest = {
            "IsEncrypted": self.encrypted,
            "Version": "10.0",
            "Date": when,
            "SystemDomainsVersion": "24.0",
            "WasPasscodeSet": True,
            "Lockdown": lockdown,
            "Applications": {},
        }
        (root / "Manifest.plist").write_bytes(plistlib.dumps(manifest, fmt=plistlib.FMT_BINARY))
        if self.status is not None:
            status = {
                "BackupState": "new",
                "Date": when,
                "IsFullBackup": False,
                "SnapshotState": self.status,
                "UUID": "6A5B4C3D-2E1F-4A0B-9C8D-7E6F5A4B3C2D",
                "Version": "3.3",
            }
            (root / "Status.plist").write_bytes(plistlib.dumps(status, fmt=plistlib.FMT_BINARY))
        if not self.manifest_db:
            return root
        if self.encrypted:  # an encrypted Manifest.db is just noise without the password
            (root / "Manifest.db").write_bytes(hashlib.sha256(b"noise").digest() * 256)
            return root

        db = sqlite3.connect(root / "Manifest.db")
        db.executescript(
            """
            CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB);
            CREATE INDEX FilesDomainIdx ON Files(domain);
            CREATE INDEX FilesRelativePathIdx ON Files(relativePath);
            CREATE INDEX FilesFlagsIdx ON Files(flags);
            CREATE TABLE Properties (key TEXT PRIMARY KEY, value BLOB);
            """
        )
        modified = int(self.when.timestamp()) - 86_400
        for entry in self.entries:
            fid = entry.file_id or hashlib.sha1(f"{entry.domain}-{entry.path}".encode()).hexdigest()
            mode = 0o100644 if entry.flags == 1 else 0o040755
            blob = entry.blob if entry.blob is not None else mbfile(entry.path, len(entry.data), modified, mode)
            db.execute("INSERT INTO Files VALUES (?, ?, ?, ?, ?)", (fid, entry.domain, entry.path, entry.flags, blob))
            if entry.on_disk:
                stored = root / fid[:2] / fid
                stored.parent.mkdir(exist_ok=True)
                stored.write_bytes(entry.data)
        db.commit()
        db.close()
        return root


CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
ICLOUD = "PhotoData/CPLAssets/group349/6F1E2D3C-4B5A-4978-8695-A4B3C2D1E0F9.JPG"


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


@dataclass
class Row:
    """One photo or video in a fake Photos library."""

    directory: str | None
    filename: str | None
    kind: int = 0
    taken: datetime | None = None
    place: tuple[float, float] = (-180.0, -180.0)  # how Photos stores "no location"
    trashed_on: datetime | None = None  # set: in Recently Deleted
    hidden: bool = False
    favorite: bool = False
    original_name: str | None = None


def _core_data(moment: datetime | None) -> float | None:
    return None if moment is None else (moment - CORE_DATA_EPOCH).total_seconds()


def photos_database(rows: list[Row], table: str = "ZASSET", keep_in_log: bool = False) -> dict[str, bytes]:
    """A Photos.sqlite with the columns afterimage reads, as {file name: contents}.

    With keep_in_log, the rows are only in the write-ahead log (Photos.sqlite-wal), not yet in the database
    file itself, as can happen on a phone.
    """
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "Photos.sqlite"
        db = sqlite3.connect(path)
        db.executescript(
            f"""
            CREATE TABLE {table} (
                Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZUUID VARCHAR, ZDIRECTORY VARCHAR, ZFILENAME VARCHAR,
                ZKIND INTEGER, ZDATECREATED TIMESTAMP, ZADDEDDATE TIMESTAMP, ZLATITUDE FLOAT, ZLONGITUDE FLOAT,
                ZTRASHEDSTATE INTEGER, ZTRASHEDDATE TIMESTAMP, ZHIDDEN INTEGER, ZFAVORITE INTEGER,
                ZADDITIONALATTRIBUTES INTEGER
            );
            CREATE TABLE ZADDITIONALASSETATTRIBUTES (
                Z_PK INTEGER PRIMARY KEY, ZASSET INTEGER, ZORIGINALFILENAME VARCHAR, ZTIMEZONENAME VARCHAR
            );
            """
        )
        if keep_in_log:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA wal_autocheckpoint = 0")
        for pk, row in enumerate(rows, start=1):
            db.execute(
                f"INSERT INTO {table} VALUES (?, 33, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pk,
                    f"UUID-{pk}",
                    row.directory,
                    row.filename,
                    row.kind,
                    _core_data(row.taken),
                    _core_data(row.taken),
                    *row.place,
                    1 if row.trashed_on else 0,
                    _core_data(row.trashed_on),
                    int(row.hidden),
                    int(row.favorite),
                    pk,
                ),
            )
            db.execute(
                "INSERT INTO ZADDITIONALASSETATTRIBUTES VALUES (?, ?, ?, 'America/Winnipeg')",
                (pk, pk, row.original_name or row.filename),
            )
        db.commit()
        files = {"Photos.sqlite": path.read_bytes()}
        log = path.with_name("Photos.sqlite-wal")
        if keep_in_log and log.exists():  # read while still open: closing moves the rows into the database
            files[log.name] = log.read_bytes()
        db.close()
    return files


LIBRARY = [
    Row("DCIM/100APPLE", "IMG_0001.HEIC", taken=utc(2026, 10, 18, 18, 2), place=(49.8951, -97.1384)),
    Row("DCIM/100APPLE", "IMG_0002.JPG", taken=utc(2026, 10, 18, 19, 30), favorite=True),
    Row("DCIM/100APPLE", "IMG_0003.MOV", kind=1, taken=utc(2026, 10, 19, 1, 10), trashed_on=utc(2026, 10, 19, 22, 0)),
    # IMG_0004.HEIC has no row: it was deleted for good, and only its thumbnails are left.
    Row("DCIM/100APPLE", "IMG_0005.HEIC", taken=utc(2026, 10, 17, 15, 45), trashed_on=utc(2026, 10, 20, 12, 0)),
    Row(
        ICLOUD.rpartition("/")[0],
        ICLOUD.rpartition("/")[2],
        taken=utc(2025, 7, 1, 16, 20),
        place=(51.0447, -114.0719),
        hidden=True,
        original_name="IMG_4821.JPG",  # iCloud files get new names; the database keeps the camera's
    ),
]


def add_camera_roll(fake: FakeBackup, library: list[Row] | None = LIBRARY, keep_in_log: bool = True) -> FakeBackup:
    """A camera roll with its Photos library. Three photos and videos are in the backup with their thumbnails.

    Three more have thumbnails but no original: IMG_0004 was deleted for good, IMG_0005 is in Recently
    Deleted, and the iCloud one is still in the library. IMG_0003 is in Recently Deleted but still here.
    """
    fake.folder(CAMERA_ROLL, "Media").folder(CAMERA_ROLL, "Media/DCIM").folder(CAMERA_ROLL, "Media/DCIM/100APPLE")
    fake.file(CAMERA_ROLL, "Media/DCIM/100APPLE/IMG_0001.HEIC", HEIC)
    fake.file(CAMERA_ROLL, "Media/DCIM/100APPLE/IMG_0002.JPG", JPEG * 40)
    fake.file(CAMERA_ROLL, "Media/DCIM/100APPLE/IMG_0002.AAE", b"<plist>edit settings</plist>")
    fake.file(CAMERA_ROLL, "Media/DCIM/100APPLE/IMG_0003.MOV", MOV)
    previews = {
        "DCIM/100APPLE/IMG_0001.HEIC": ["5005", "5003"],
        "DCIM/100APPLE/IMG_0002.JPG": ["5005"],
        "DCIM/100APPLE/IMG_0003.MOV": ["5005"],
        "DCIM/100APPLE/IMG_0004.HEIC": ["5005", "5003"],
        "DCIM/100APPLE/IMG_0005.HEIC": ["5005"],
        ICLOUD: ["5003"],
    }
    for original, variants in previews.items():
        for variant in variants:
            fake.file(CAMERA_ROLL, f"Media/PhotoData/Thumbnails/V2/{original}/{variant}.JPG", JPEG)
    fake.file(CAMERA_ROLL, "Media/PhotoData/Thumbnails/V2/thumbnailConfiguration", b"bplist00")
    fake.file(CAMERA_ROLL, "Media/PhotoData/Thumbnails/3303.ithmb", bytes(512))
    if library is not None:
        for name, data in photos_database(library, keep_in_log=keep_in_log).items():
            fake.file(CAMERA_ROLL, f"Media/PhotoData/{name}", data)
    fake.file(CAMERA_ROLL, "Media/PhotoData/Mutations/DCIM/100APPLE/IMG_0002/Adjustments/FullSizeRender.jpg", JPEG)
    fake.file("HomeDomain", "Library/SMS/sms.db", b"SQLite format 3\x00")
    fake.file("AppDomain-com.example.notes", "Documents/notes.json", b"{}")
    return fake


def snapshot(folder: Path) -> dict[str, tuple[str | None, int]]:
    """Every file's contents and every file's and folder's modified time, to prove nothing was written.

    Folder times matter too: a file created and then deleted (say, an SQLite journal) still changes them.
    """
    state: dict[str, tuple[str | None, int]] = {".": (None, folder.stat().st_mtime_ns)}
    for path in sorted(folder.rglob("*")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        state[path.relative_to(folder).as_posix()] = (digest, path.stat().st_mtime_ns)
    return state


@pytest.fixture
def backups_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "Backup"
    folder.mkdir()
    return folder


@pytest.fixture
def phone(backups_dir: Path) -> Path:
    """A finished, unencrypted backup of "Om's iPhone" with a camera roll."""
    return add_camera_roll(FakeBackup(backups_dir)).build()
