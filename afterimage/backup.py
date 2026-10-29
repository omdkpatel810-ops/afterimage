"""Find and read local iPhone backups: the folders that Finder, iTunes and the Apple Devices app make.

A backup folder (iOS 10 and later) holds:

- Info.plist, Manifest.plist and Status.plist: the device, whether the backup is encrypted, whether it finished.
- Manifest.db: an SQLite table with one row per file or folder on the phone: its "domain" (the app or part
  of the system it belongs to), its path there, and an archived record of its size and dates.
- The files themselves, each named after the SHA-1 of "<domain>-<path>" and kept in a subfolder named after
  the first two characters of that name.

Everything here only reads. Manifest.db is opened read-only and as "immutable", so SQLite never writes to
it, not even a lock or journal file. An encrypted backup is opened with its password (see crypto.py): its
Manifest.db is decrypted into a private temporary folder that's deleted when the backup is closed, and each
file is decrypted as it's read.
"""

from __future__ import annotations

import hashlib
import os
import platform
import plistlib
import re
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path
from typing import Any

from . import crypto
from .keyedarchive import ArchiveError, unarchive

_FILE_ID = re.compile(r"[0-9a-f]{40}")
_KINDS = {1: "file", 2: "folder", 4: "link"}


class BackupError(Exception):
    """Something stops the backup being read; the message says what, in plain words."""


class PasswordNeeded(BackupError):
    """The backup is encrypted, and opening it needs its backup password."""


def file_id(domain: str, relative_path: str) -> str:
    """The name a file is stored under in a backup: the SHA-1 of "<domain>-<relative path>"."""
    return hashlib.sha1(f"{domain}-{relative_path}".encode(), usedforsecurity=False).hexdigest()


def default_backup_dirs(
    system: str | None = None, home: Path | None = None, environ: Mapping[str, str] | None = None
) -> list[Path]:
    """Where Apple's tools keep backups on this computer. Linux has no standard place, so none."""
    system = system or platform.system()
    home = home or Path.home()
    environ = os.environ if environ is None else environ
    if system == "Darwin":
        return [home / "Library" / "Application Support" / "MobileSync" / "Backup"]
    if system == "Windows":
        dirs = []
        if environ.get("APPDATA"):  # iTunes downloaded from Apple
            dirs.append(Path(environ["APPDATA"]) / "Apple Computer" / "MobileSync" / "Backup")
        dirs.append(home / "Apple" / "MobileSync" / "Backup")  # the Apple Devices app and the Store's iTunes
        return dirs
    return []


def blocked_message(path: Path) -> str:
    message = f"This computer didn't allow reading {path}."
    if platform.system() == "Darwin":
        message += (
            " On a Mac, give your terminal app Full Disk Access (System Settings > Privacy & Security >"
            " Full Disk Access), then open a new terminal window and try again."
        )
    return message


@dataclass(frozen=True)
class BackupInfo:
    """What a backup's plists say about it. Reading these works even when the backup is encrypted."""

    path: Path
    device_name: str | None
    model: str | None
    ios_version: str | None
    last_backup: datetime | None
    encrypted: bool
    finished: bool | None  # None when the backup has no Status.plist

    @property
    def name(self) -> str:
        return self.device_name or self.path.name


def read_info(path: str | os.PathLike[str]) -> BackupInfo:
    """Read a backup folder's Info.plist, Manifest.plist and Status.plist."""
    path = Path(path)
    if not path.is_dir():
        raise BackupError(f"No folder at {path}")
    manifest = _load_plist(path / "Manifest.plist")
    if manifest is None:
        raise BackupError(f"{path} isn't an iPhone backup: it has no Manifest.plist")
    info = _load_plist(path / "Info.plist") or {}
    status = _load_plist(path / "Status.plist")
    lockdown = manifest.get("Lockdown") if isinstance(manifest.get("Lockdown"), dict) else {}
    return BackupInfo(
        path=path,
        device_name=_text(info.get("Device Name"), info.get("Display Name"), lockdown.get("DeviceName")),
        model=_text(info.get("Product Type"), lockdown.get("ProductType")),
        ios_version=_text(info.get("Product Version"), lockdown.get("ProductVersion")),
        last_backup=_utc(info.get("Last Backup Date"))
        or _utc((status or {}).get("Date"))
        or _utc(manifest.get("Date")),
        encrypted=manifest.get("IsEncrypted") is True,
        finished=None if status is None else status.get("SnapshotState") == "finished",
    )


@dataclass(frozen=True)
class Search:
    """The backups found in some folders, and the folders that looked like backups but couldn't be read."""

    searched: list[Path]
    backups: list[BackupInfo] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)


def find_backups(dirs: Iterable[str | os.PathLike[str]]) -> Search:
    """Look for backups in each folder (or treat the folder itself as one). Newest first."""
    search = Search(searched=[Path(d) for d in dirs])
    for folder in search.searched:
        try:
            if (folder / "Manifest.plist").is_file():
                candidates = [folder]
            elif folder.is_dir():
                candidates = sorted(child for child in folder.iterdir() if (child / "Manifest.plist").is_file())
            else:
                candidates = []
        except PermissionError as error:
            raise BackupError(blocked_message(folder)) from error
        for candidate in candidates:
            try:
                search.backups.append(read_info(candidate))
            except BackupError as error:
                search.skipped.append((candidate, str(error)))
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    search.backups.sort(key=lambda b: b.last_backup or oldest, reverse=True)
    return search


@dataclass(frozen=True)
class BackupFile:
    """One row of Manifest.db: a file, folder or link on the phone.

    Its size, dates and key are in an archived record that's only decoded when one of them is asked for:
    decoding is most of the work in reading a big backup, and sorting a camera roll needs only the paths.
    """

    file_id: str
    domain: str
    relative_path: str
    kind: str  # "file", "folder", "link" or "other"
    record: bytes | None = field(default=None, repr=False, compare=False)

    @property
    def name(self) -> str:
        return self.relative_path.rpartition("/")[2]

    @property
    def size(self) -> int | None:
        return self._details[0]

    @property
    def modified(self) -> datetime | None:
        return self._details[1]

    @property
    def protection_class(self) -> int | None:
        """Encrypted backups only: which class key unwraps this file's key."""
        return self._details[2]

    @property
    def wrapped_key(self) -> bytes | None:
        """Encrypted backups only: this file's own key, wrapped."""
        return self._details[3]

    @cached_property
    def _details(self) -> tuple[int | None, datetime | None, int | None, bytes | None]:
        return _decode_record(self.record)


class Backup:
    """A backup, open for reading. An encrypted one needs its backup password.

    >>> with Backup(path) as backup:
    ...     for f in backup.files("CameraRollDomain", "Media/DCIM/"):
    ...         data = backup.read(f)
    """

    def __init__(self, path: str | os.PathLike[str], password: str | None = None) -> None:
        self.info = read_info(path)
        self.path = self.info.path
        self._keys: dict[int, bytes] | None = None
        self._scratch: tempfile.TemporaryDirectory[str] | None = None
        manifest = self.path / "Manifest.db"
        if not manifest.is_file():
            if (self.path / "Manifest.mbdb").is_file():
                raise BackupError(
                    f"The backup in {self.path} was made by iOS 9 or earlier. afterimage reads backups from iOS 10 on."
                )
            raise BackupError(f"{self.path} has no Manifest.db, so the backup may be unfinished. Make a new backup.")
        if self.info.encrypted:
            if password is None:
                raise PasswordNeeded(
                    f"The backup of {self.info.name} is encrypted, so it needs its backup password."
                    " afterimage never tries to get around it."
                )
            manifest = self._unlock(manifest, password)
        self._db = sqlite3.connect(f"{manifest.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            self._db.execute("SELECT fileID, domain, relativePath, flags, file FROM Files LIMIT 1").fetchall()
        except sqlite3.DatabaseError as error:
            self.close()
            raise BackupError(f"Couldn't read Manifest.db in {self.path}: {error}") from error

    def _unlock(self, manifest: Path, password: str) -> Path:
        """Unlock the keybag, and decrypt Manifest.db into a private temporary folder."""
        plist = _load_plist(self.path / "Manifest.plist") or {}
        keybag, manifest_key = plist.get("BackupKeyBag"), plist.get("ManifestKey")
        if not isinstance(keybag, bytes):
            raise BackupError(f"The backup of {self.info.name} says it's encrypted, but has no keybag to unlock.")
        try:
            self._keys = crypto.unlock(crypto.parse_keybag(keybag), password)
            self._scratch = tempfile.TemporaryDirectory(prefix="afterimage-")  # readable only by this user
            plain = Path(self._scratch.name) / "Manifest.db"
            if isinstance(manifest_key, bytes) and len(manifest_key) > 4:
                protection_class = int.from_bytes(manifest_key[:4], "little")
                key = crypto.class_unwrap(self._keys, protection_class, manifest_key[4:])
                crypto.decrypt_file(key, manifest, plain)
            else:  # before iOS 10.2, the files were encrypted but their list wasn't
                shutil.copyfile(manifest, plain)
        except crypto.WrongPassword as error:
            self.close()
            raise BackupError(
                f"That password doesn't unlock the backup of {self.info.name}. It's the password chosen when"
                " \u201cEncrypt local backup\u201d was turned on, not the phone's passcode."
            ) from error
        except crypto.CryptoError as error:
            self.close()
            raise BackupError(str(error)) from error
        return plain

    def __enter__(self) -> Backup:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if getattr(self, "_db", None) is not None:
            self._db.close()
        if self._scratch is not None:
            self._scratch.cleanup()
            self._scratch = None

    @property
    def encrypted(self) -> bool:
        return self._keys is not None

    def files(self, domain: str | None = None, prefix: str = "") -> Iterator[BackupFile]:
        """Every entry, or those in one domain and/or under a path prefix (matched exactly, case and all)."""
        where, params = [], []
        if domain is not None:
            where.append("domain = ?")
            params.append(domain)
        if prefix:
            # Not LIKE: that ignores case and treats % and _ as wildcards, and paths can contain both.
            where.append("substr(relativePath, 1, ?) = ?")
            params += [len(prefix), prefix]
        sql = "SELECT fileID, domain, relativePath, flags, file FROM Files"
        if where:
            sql += " WHERE " + " AND ".join(where)
        for row in self._db.execute(sql + " ORDER BY domain, relativePath", params):
            yield _to_file(row)

    def domains(self) -> list[tuple[str, int]]:
        """Each domain with its number of entries, the biggest first."""
        rows = self._db.execute("SELECT domain, COUNT(*) FROM Files GROUP BY domain ORDER BY COUNT(*) DESC, domain")
        return [(domain or "", count) for domain, count in rows]

    def kinds(self) -> dict[str, int]:
        """How many files, folders and links the backup lists."""
        counts: dict[str, int] = {}
        for flags, count in self._db.execute("SELECT flags, COUNT(*) FROM Files GROUP BY flags"):
            kind = _KINDS.get(flags, "other")
            counts[kind] = counts.get(kind, 0) + count
        return counts

    def locate(self, entry: BackupFile) -> Path | None:
        """Where a file's contents are in the backup folder, or None if they aren't there."""
        return self.path / entry.file_id[:2] / entry.file_id if self.stat(entry) is not None else None

    def stat(self, entry: BackupFile) -> os.stat_result | None:
        """The stored file's details (size and so on), or None if it isn't in the backup folder.

        The name comes from Manifest.db, so it's checked first: anything but 40 hex digits could point
        outside the backup (think "../../").
        """
        if not _FILE_ID.fullmatch(entry.file_id):
            return None
        try:
            found = os.stat(os.path.join(self.path, entry.file_id[:2], entry.file_id))
        except OSError:
            return None
        return found if stat.S_ISREG(found.st_mode) else None

    def read(self, entry: BackupFile) -> bytes:
        """A file's contents, decrypted if the backup is encrypted."""
        source = self._source(entry)
        data = source.read_bytes()
        if self._keys is None or not data:
            return data
        try:
            return crypto.decrypt(self._file_key(entry), data)
        except crypto.CryptoError as error:
            raise BackupError(f"Couldn't decrypt {entry.relative_path}: {error}") from error

    def copy(self, entry: BackupFile, target: Path) -> None:
        """Copy a file out of the backup, decrypting it on the way if the backup is encrypted."""
        source = self._source(entry)
        if self._keys is None or source.stat().st_size == 0:
            shutil.copyfile(source, target)
            return
        try:
            crypto.decrypt_file(self._file_key(entry), source, target)
        except crypto.CryptoError as error:
            target.unlink(missing_ok=True)
            raise BackupError(f"Couldn't decrypt {entry.relative_path}: {error}") from error

    def _source(self, entry: BackupFile) -> Path:
        source = self.locate(entry)
        if source is None:
            raise BackupError(f"{entry.relative_path} is listed in the backup, but its file is missing.")
        return source

    def _file_key(self, entry: BackupFile) -> bytes:
        assert self._keys is not None
        if entry.protection_class is None or entry.wrapped_key is None:
            raise crypto.CryptoError("the backup holds no key for it")
        return crypto.class_unwrap(self._keys, entry.protection_class, entry.wrapped_key)


def _to_file(row: tuple[Any, ...]) -> BackupFile:
    fid, domain, relative_path, flags, blob = row
    record = blob if isinstance(blob, bytes) else None
    return BackupFile(str(fid or ""), str(domain or ""), str(relative_path or ""), _KINDS.get(flags, "other"), record)


def _decode_record(record: bytes | None) -> tuple[int | None, datetime | None, int | None, bytes | None]:
    """Size, modified time, protection class and wrapped key from a file's archived MBFile record."""
    try:
        details = unarchive(record) if record is not None else None
    except ArchiveError:
        details = None
    if not isinstance(details, dict):
        return None, None, None, None
    size, protection_class, key = details.get("Size"), details.get("ProtectionClass"), details.get("EncryptionKey")
    return (
        size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
        _from_unix(details.get("LastModified")),
        protection_class if isinstance(protection_class, int) and not isinstance(protection_class, bool) else None,
        key[4:] if isinstance(key, bytes) and len(key) > 4 else None,  # 4 bytes of class, then the wrapped key
    )


def _load_plist(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file():
            return None
        with path.open("rb") as handle:
            value = plistlib.load(handle)
    except PermissionError as error:
        raise BackupError(blocked_message(path)) from error
    except Exception as error:  # plistlib raises several kinds of error for bad input
        raise BackupError(f"Couldn't read {path.name} in {path.parent}: it isn't a valid plist") from error
    if not isinstance(value, dict):
        raise BackupError(f"{path.name} in {path.parent} isn't shaped like the one in an iPhone backup")
    return value


def _text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _from_unix(value: Any) -> datetime | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
