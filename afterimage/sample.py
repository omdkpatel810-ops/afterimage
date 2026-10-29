"""Build backup folders shaped like the ones Finder and iTunes make, for the tests and the demo.

Nothing here touches a real phone. A sample backup has the same layout as a real one (iOS 10 and later):
the three plists, Manifest.db with an archived record per file, files stored under the SHA-1 of their
domain and path, and, optionally, a Photos.sqlite. It can also be encrypted the way Apple encrypts backups,
with a keybag unlocked by a password (that part needs the `cryptography` package).
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import sqlite3
import struct
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .library import CORE_DATA_EPOCH

SAMPLE_UDID = "00008110-0000A11CE0000000"


def mbfile(
    path: str,
    size: int,
    modified: int,
    mode: int = 0o100644,
    protection_class: int = 3,
    encryption_key: bytes | None = None,
) -> bytes:
    """The NSKeyedArchiver record Manifest.db keeps for each file, in the shape Apple's tools write."""
    uid = plistlib.UID
    record: dict[str, Any] = {
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
        "ProtectionClass": protection_class,
        "Flags": 0,
    }
    objects: list[Any] = ["$null", record, path, {"$classname": "MBFile", "$classes": ["MBFile", "NSObject"]}]
    if encryption_key is not None:  # NSData: the protection class (4 bytes, little-endian), then the wrapped key
        record["EncryptionKey"] = uid(4)
        objects += [
            {"$class": uid(5), "NS.data": encryption_key},
            {"$classname": "NSMutableData", "$classes": ["NSMutableData", "NSData", "NSObject"]},
        ]
    archive = {"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": uid(1)}, "$objects": objects}
    return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)


@dataclass
class Encryption:
    """How a sample backup is encrypted. The round counts are tiny so tests stay fast; real ones are huge."""

    password: str
    dpic: int | None = 10  # None: an older keybag, from before the SHA-256 round was added
    iterations: int = 10
    manifest_key: bool = True  # False: before iOS 10.2, when Manifest.db itself wasn't encrypted
    protection_class: int = 3
    device_only: bool = False  # True: every class key needs the phone's hardware key, not the password
    keys: dict[int, bytes] = field(default_factory=dict, repr=False)


@dataclass
class _Entry:
    domain: str
    path: str
    data: bytes
    flags: int
    on_disk: bool
    blob: bytes | None
    file_id: str | None


class SampleBackup:
    """Builds a backup folder. Add files and folders, then call build()."""

    def __init__(
        self,
        parent: Path,
        udid: str = SAMPLE_UDID,
        device: str | None = "Sample iPhone",
        model: str = "iPhone15,2",
        ios: str = "17.5.1",
        when: datetime | None = None,
    ) -> None:
        self.parent, self.udid, self.device, self.model, self.ios = parent, udid, device, model, ios
        self.when = when or datetime(2026, 10, 20, 21, 14, tzinfo=timezone.utc)
        self.encrypted = False  # True without `encryption`: marked encrypted, with a Manifest.db of noise
        self.encryption: Encryption | None = None
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
    ) -> SampleBackup:
        self.entries.append(_Entry(domain, path, data, 1, on_disk, blob, file_id))
        return self

    def folder(self, domain: str, path: str) -> SampleBackup:
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
        manifest: dict[str, Any] = {
            "IsEncrypted": self.encrypted or self.encryption is not None,
            "Version": "10.0",
            "Date": when,
            "SystemDomainsVersion": "24.0",
            "WasPasscodeSet": True,
            "Lockdown": lockdown,
            "Applications": {},
        }
        lock = self.encryption
        if lock is not None:
            manifest["BackupKeyBag"] = _keybag(lock)
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
            (root / "Manifest.plist").write_bytes(plistlib.dumps(manifest, fmt=plistlib.FMT_BINARY))
            return root
        if self.encrypted and lock is None:  # an encrypted Manifest.db is noise without the password
            (root / "Manifest.plist").write_bytes(plistlib.dumps(manifest, fmt=plistlib.FMT_BINARY))
            (root / "Manifest.db").write_bytes(hashlib.sha256(b"noise").digest() * 256)
            return root

        with tempfile.TemporaryDirectory() as scratch:
            listing = Path(scratch) / "Manifest.db"
            db = sqlite3.connect(listing)
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
                stored, wrapped = entry.data, None
                if lock is not None and entry.flags == 1:
                    key = os.urandom(32)
                    wrapped = struct.pack("<I", lock.protection_class) + _wrap(lock.keys[lock.protection_class], key)
                    stored = encrypt(key, entry.data)
                blob = entry.blob
                if blob is None:
                    blob = mbfile(
                        entry.path, len(entry.data), modified, mode, lock.protection_class if lock else 3, wrapped
                    )
                db.execute(
                    "INSERT INTO Files VALUES (?, ?, ?, ?, ?)", (fid, entry.domain, entry.path, entry.flags, blob)
                )
                if entry.on_disk:
                    target = root / fid[:2] / fid
                    target.parent.mkdir(exist_ok=True)
                    target.write_bytes(stored)
            db.commit()
            db.close()
            listed = listing.read_bytes()
        if lock is not None and lock.manifest_key:
            key = os.urandom(32)
            manifest["ManifestKey"] = struct.pack("<I", 4) + _wrap(lock.keys[4], key)
            listed = encrypt(key, listed)
        (root / "Manifest.db").write_bytes(listed)
        (root / "Manifest.plist").write_bytes(plistlib.dumps(manifest, fmt=plistlib.FMT_BINARY))
        return root


@dataclass
class Row:
    """One photo or video in a sample Photos library."""

    directory: str | None
    filename: str | None
    kind: int = 0
    taken: datetime | None = None
    place: tuple[float, float] = (-180.0, -180.0)  # how Photos stores "no location"
    trashed_on: datetime | None = None  # set: in Recently Deleted
    hidden: bool = False
    favorite: bool = False
    original_name: str | None = None


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


def encrypt(key: bytes, data: bytes) -> bytes:
    """AES-256-CBC with a zero IV and PKCS #7 padding, as backups encrypt each file."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padder = padding.PKCS7(128).padder()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(bytes(16))).encryptor()
    return encryptor.update(padder.update(data) + padder.finalize()) + encryptor.finalize()


def _wrap(kek: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives import keywrap

    return keywrap.aes_key_wrap(kek, key)


def _keybag(lock: Encryption) -> bytes:
    """A backup keybag holding class keys 1 to 11, wrapped with a key made from the password."""
    salt, dp_salt = os.urandom(20), os.urandom(20)
    secret = lock.password.encode("utf-8")
    entries = [(b"VERS", _u32(4)), (b"TYPE", _u32(1)), (b"UUID", os.urandom(16)), (b"HMCK", os.urandom(40))]
    entries += [(b"WRAP", _u32(0)), (b"SALT", salt), (b"ITER", _u32(lock.iterations))]
    if lock.dpic is not None:
        entries += [(b"DPWT", _u32(1)), (b"DPIC", _u32(lock.dpic)), (b"DPSL", dp_salt)]
        secret = hashlib.pbkdf2_hmac("sha256", secret, dp_salt, lock.dpic, 32)
    password_key = hashlib.pbkdf2_hmac("sha1", secret, salt, lock.iterations, 32)
    lock.keys = {protection_class: os.urandom(32) for protection_class in range(1, 12)}
    for protection_class, key in lock.keys.items():
        wrapper, flag = (os.urandom(32), 1) if lock.device_only else (password_key, 2)
        entries += [(b"UUID", os.urandom(16)), (b"CLAS", _u32(protection_class)), (b"WRAP", _u32(flag))]
        entries += [(b"KTYP", _u32(0)), (b"WPKY", _wrap(wrapper, key))]
    return b"".join(tag + struct.pack(">I", len(value)) + value for tag, value in entries)


def _u32(number: int) -> bytes:
    return struct.pack(">I", number)


def _naive_utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _core_data(moment: datetime | None) -> float | None:
    return None if moment is None else (moment - CORE_DATA_EPOCH).total_seconds()
