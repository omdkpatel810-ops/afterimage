"""The afterimage command: find backups, describe one, and count what its camera roll holds."""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import TextIO

from . import __version__
from .backup import Backup, BackupError, BackupInfo, PasswordNeeded, default_backup_dirs, find_backups, read_info
from .camera_roll import CameraRoll, scan
from .export import FOLDERS, SAVED_LABELS, TABLE, ExportError, export
from .library import (
    HEADINGS,
    LABELS,
    RECENTLY_DELETED,
    STATUSES,
    Asset,
    Leftover,
    Library,
    LibraryError,
    by_status,
    find_leftovers,
    read_library,
    recently_deleted_in_backup,
)

HOW_TO_BACK_UP = (
    "To make one, connect your iPhone to this computer, open Finder (Mac) or the Apple Devices app or iTunes"
    " (Windows), and back it up to this computer."
)
NO_STANDARD_PLACE = (
    "This system has no standard place for iPhone backups (on Linux, libimobiledevice's idevicebackup2"
    " makes them wherever you say). Give the backup folder's path, or the folder that holds backups with --dir."
)


PasswordSource = Callable[[BackupInfo], "str | None"]


@dataclass(frozen=True)
class Context:
    out: TextIO
    dirs: list[Path]
    tz: tzinfo | None
    err: TextIO
    password: PasswordSource


def main(
    argv: Sequence[str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    backup_dirs: Sequence[str | Path] | None = None,
    tz: tzinfo | None = None,
    ask_password: PasswordSource | None = None,
    stdin: TextIO | None = None,
) -> int:
    """Run the command line. The keyword arguments let tests swap the screen, folders, time zone and password."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = _parser().parse_args(argv)
    dirs = args.dir or (default_backup_dirs() if backup_dirs is None else backup_dirs)
    password = ask_password or _ask_password
    if getattr(args, "password_stdin", False):
        reader = sys.stdin if stdin is None else stdin

        def password(info: BackupInfo) -> str:
            return reader.readline().rstrip("\r\n")

    try:
        return args.run(args, Context(out, [Path(d).expanduser() for d in dirs], tz, err, password))
    except (BackupError, ExportError) as error:
        print(f"error: {error}", file=err)
        return 1
    except KeyboardInterrupt:
        print(file=err)
        return 130


def _ask_password(info: BackupInfo) -> str:  # pragma: no cover - needs a person at a terminal
    return getpass.getpass(f"Backup password for {info.name} (not the phone's passcode): ")


def _open(path: Path, ctx: Context) -> Backup:
    """Open a backup, asking for its password if it's encrypted. The password isn't kept."""
    try:
        return Backup(path)
    except PasswordNeeded:
        info = read_info(path)
        password = ctx.password(info)
        if not password:
            raise BackupError(f"The backup of {info.name} is encrypted, and no password was given.") from None
        print("Unlocking the backup. This takes a few seconds.", file=ctx.err)
        return Backup(path, password)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="afterimage", description="Find the photos your iPhone backup still remembers. Only ever reads."
    )
    parser.add_argument("--version", action="version", version=f"afterimage {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    where = argparse.ArgumentParser(add_help=False)
    where.add_argument(
        "--dir",
        action="append",
        metavar="FOLDER",
        help="a folder that holds backups, instead of the usual place (can be given more than once)",
    )
    which = argparse.ArgumentParser(add_help=False)
    which.add_argument(
        "backup",
        nargs="?",
        help="the backup's folder, folder name, or device name (can be left out if there's only one backup)",
    )

    secret = argparse.ArgumentParser(add_help=False)
    secret.add_argument(
        "--password-stdin",
        action="store_true",
        help="read an encrypted backup's password from the first line of standard input, instead of asking",
    )

    listing = commands.add_parser("list", parents=[where], help="list the backups on this computer")
    listing.set_defaults(run=_list)
    info = commands.add_parser("info", parents=[which, where], help="show a backup's device, date and contents")
    info.set_defaults(run=_info)
    photos = commands.add_parser(
        "photos", parents=[which, where, secret], help="count the photos and thumbnails a backup holds"
    )
    photos.add_argument("--list", action="store_true", help="list the thumbnails whose original isn't in the backup")
    photos.set_defaults(run=_photos)
    saving = commands.add_parser(
        "export", parents=[which, where, secret], help="save the leftover thumbnails, with a report page to open"
    )
    saving.add_argument(
        "--out", required=True, metavar="FOLDER", help="a new or empty folder to save them in, outside the backup"
    )
    saving.set_defaults(run=_export)
    return parser


# Commands ----------------------------------------------------------------------------------------------


def _list(args: argparse.Namespace, ctx: Context) -> int:
    if not ctx.dirs:
        raise BackupError(NO_STANDARD_PLACE)
    search = find_backups(ctx.dirs)
    where = _join(search.searched)
    if not search.backups:
        print(f"No backups found in {where}.", file=ctx.out)
        print(HOW_TO_BACK_UP, file=ctx.out)
    else:
        print(f"{_plural(len(search.backups), 'backup')} in {where}:", file=ctx.out)
        rows = [
            [
                backup.name,
                f"iOS {backup.ios_version or '?'}",
                _when(backup.last_backup, ctx.tz),
                ", ".join(_flags(backup)) or "-",
                backup.path.name,
            ]
            for backup in search.backups
        ]
        for line in _table(rows):
            print("  " + line, file=ctx.out)
    for path, reason in search.skipped:
        print(f"Skipped {path}: {reason}", file=ctx.out)
    return 0


def _info(args: argparse.Namespace, ctx: Context) -> int:
    info = read_info(_resolve(args.backup, ctx))
    rows = [
        ["Model", info.model or "unknown"],
        ["iOS", info.ios_version or "unknown"],
        ["Backed up", _when(info.last_backup, ctx.tz)],
        ["Encrypted", "yes" if info.encrypted else "no"],
        ["Finished", {True: "yes", False: "no", None: "unknown"}[info.finished]],
        ["Folder", str(info.path)],
    ]
    domains: list[tuple[str, int]] = []
    if not info.encrypted:
        with Backup(info.path) as backup:
            kinds, domains = backup.kinds(), backup.domains()
        rows.append(
            [
                "Contents",
                f"{_plural(kinds.get('file', 0), 'file')} and {_plural(kinds.get('folder', 0), 'folder')}"
                f" from {_plural(len(domains), 'app or system area', 'apps and system areas')}",
            ]
        )

    print(info.name, file=ctx.out)
    for line in _table(rows):
        print("  " + line, file=ctx.out)
    if info.encrypted:
        print(
            "\nIt's encrypted. afterimage photos and afterimage export ask for its backup password to open it.",
            file=ctx.out,
        )
    if domains:
        print("\nAreas with the most entries:", file=ctx.out)
        for line in _table([[domain, f"{count:,}"] for domain, count in domains[:8]], right={1}):
            print("  " + line, file=ctx.out)
    _warn_if_unfinished(info, ctx)
    return 0


def _photos(args: argparse.Namespace, ctx: Context) -> int:
    with _open(_resolve(args.backup, ctx), ctx) as backup:
        info, roll = backup.info, scan(backup)
        library, problem = _read_library(backup, roll)

    details = [info.name, f"iOS {info.ios_version or '?'}", f"backed up {_when(info.last_backup, ctx.tz)}"]
    print(" · ".join(details), file=ctx.out)
    _warn_if_unfinished(info, ctx)
    if roll.total == 0:
        print("\nThis backup holds no camera roll files.", file=ctx.out)
        return 0

    previews = sum(len(found) for found in roll.thumbnails.values())
    rows = [
        [
            "Photos and videos",
            f"{len(roll.originals):,}",
            f"{_plural(roll.count('photo'), 'photo')}, {_plural(roll.count('video'), 'video')},"
            f" {_size(roll.originals_size)}",
        ],
        ["Thumbnails", f"{previews:,}", f"for {_plural(len(roll.thumbnails), 'photo or video', 'photos and videos')}"],
        [
            "Thumbnail tables",
            f"{len(roll.thumbnail_tables):,}",
            ".ithmb files, many thumbnails in each" if roll.thumbnail_tables else "",
        ],
        ["Edited versions", f"{len(roll.edits):,}", ""],
        _database_row(roll.has_database, library),
        ["Other files", f"{len(roll.other):,}", ""],
    ]
    print("\nCamera roll in this backup:", file=ctx.out)
    for line in _table(rows, right={1}):
        print("  " + line, file=ctx.out)

    leftovers = find_leftovers(roll, library)
    full_size = recently_deleted_in_backup(roll, library)
    groups = by_status(leftovers)
    print(file=ctx.out)
    if not roll.thumbnails:
        print("There are no thumbnails in Media/PhotoData/Thumbnails/V2 in this backup.", file=ctx.out)
    elif not leftovers:
        print("Every thumbnail's photo or video is in this backup too.", file=ctx.out)
    elif library is None:
        verb = "has" if len(leftovers) == 1 else "have"
        print(
            f"{_plural(len(leftovers), 'photo or video', 'photos and videos')} {verb} thumbnails but no original"
            " in this backup.\nEach was deleted, or is kept in iCloud and the phone held only a small copy."
            f"\n{problem} So afterimage can't tell which.",
            file=ctx.out,
        )
    else:
        print(
            f"{_plural(len(leftovers), 'photo or video', 'photos and videos')} with thumbnails but no original"
            " in this backup:",
            file=ctx.out,
        )
        rows = [[LABELS[status], f"{len(groups[status]):,}"] for status in STATUSES if status in groups]
        for line in _table(rows, right={1}):
            print("  " + line, file=ctx.out)
    if full_size:
        verb = "is" if len(full_size) == 1 else "are"
        print(
            f"\n{_plural(len(full_size), 'photo or video', 'photos and videos')} in Recently Deleted {verb}"
            " still in this backup at full size.",
            file=ctx.out,
        )

    if args.list:
        for status in STATUSES:
            if status in groups:
                print(f"\n{HEADINGS[status]} ({len(groups[status]):,}):", file=ctx.out)
                found = [[g.original.removeprefix("Media/"), _describe(g.asset, g, ctx.tz)] for g in groups[status]]
                for line in _table(found):
                    print("  " + line, file=ctx.out)
        if full_size:
            print(f"\nIn Recently Deleted, still in this backup at full size ({len(full_size):,}):", file=ctx.out)
            for line in _table([[a.path.removeprefix("Media/"), _describe(a, None, ctx.tz)] for a in full_size]):
                print("  " + line, file=ctx.out)
    elif leftovers or full_size:
        print("Add --list to see them, or save them with: afterimage export --out FOLDER", file=ctx.out)
    if RECENTLY_DELETED in groups or full_size:
        print(
            "\nRecently Deleted keeps photos for about 30 days. Until then, the phone can restore them at full"
            " size from that album in the Photos app.",
            file=ctx.out,
        )
    if roll.not_on_disk:
        print(
            f"\nNote: {_plural(len(roll.not_on_disk), 'camera roll file')} listed in Manifest.db"
            f" {'is' if len(roll.not_on_disk) == 1 else 'are'} missing from the backup folder,"
            " so the backup may be incomplete.",
            file=ctx.out,
        )
    return 0


def _export(args: argparse.Namespace, ctx: Context) -> int:
    from .report import ORDER

    with _open(_resolve(args.backup, ctx), ctx) as backup:
        roll = scan(backup)
        library, problem = _read_library(backup, roll)
        if not find_leftovers(roll, library) and not recently_deleted_in_backup(roll, library):
            print(
                "Nothing to save: every thumbnail's photo or video is in this backup, and nothing in it is in"
                " Recently Deleted.",
                file=ctx.out,
            )
            return 0
        result = export(backup, roll, library, Path(args.out).expanduser(), ctx.tz)

    counts = {status: sum(1 for item in result.saved if item.status == status) for status in ORDER}
    print(f"Saved {_plural(len(result.saved), 'file')} to {result.folder}:", file=ctx.out)
    rows = [[SAVED_LABELS[s], f"{n:,}", FOLDERS[s] + "/"] for s, n in counts.items() if n]
    for line in _table(rows, right={1}) if rows else []:
        print("  " + line, file=ctx.out)
    tagged = sum(1 for item in result.saved if item.tagged)
    print(f"Wrote the date taken or place into {_plural(tagged, 'thumbnail')}.", file=ctx.out)
    if problem:
        print(f"{problem} So afterimage couldn't tell what became of each photo.", file=ctx.out)
    if result.missing:
        print(
            f"{_plural(len(result.missing), 'photo or video', 'photos and videos')} couldn't be saved:"
            " their files are missing from the backup folder, or damaged.",
            file=ctx.out,
        )
    print(
        f"\nOpen report.html in your browser to see them all; {TABLE} lists them for a spreadsheet."
        "\nThis folder holds copies of your photos. Keep it private, and delete it when you're done.",
        file=ctx.out,
    )
    return 0


def _read_library(backup: Backup, roll: CameraRoll) -> tuple[Library | None, str | None]:
    """The Photos library, or None and why not. Not needed when there are no thumbnails to explain."""
    if not (roll.thumbnails or roll.has_database):
        return None, None
    try:
        return read_library(backup, roll), None
    except LibraryError as error:
        return None, str(error)


def _database_row(found: bool, library: Library | None) -> list[str]:
    if library is None:
        return ["Photos database", "unreadable" if found else "missing", ""]
    parts = [_plural(len(library.assets), "photo or video", "photos and videos")]
    if trashed := library.count(trashed=True):
        parts.append(f"{trashed:,} in Recently Deleted")
    if hidden := library.count(hidden=True):
        parts.append(f"{hidden:,} hidden")
    return ["Photos database", "found", ", ".join(parts)]


def _describe(asset: Asset | None, leftover: Leftover | None, tz: tzinfo | None) -> str:
    """One line about a photo: what the library knows about it, and what its thumbnails say."""
    parts = []
    if asset is not None:
        if asset.kind:
            parts.append(asset.kind)
        if asset.taken:
            parts.append(f"taken {_when(asset.taken, tz)}")
        if asset.place:
            parts.append(f"at {asset.place[0]:.5f}, {asset.place[1]:.5f}")
        if asset.trashed:
            parts.append(f"deleted {_when(asset.trashed_on, tz)}" if asset.trashed_on else "deleted")
        if asset.hidden:
            parts.append("hidden")
        if asset.original_name:
            parts.append(f"first named {asset.original_name}")
    if leftover is not None:
        parts.append(_plural(len(leftover.thumbnails), "thumbnail"))
        if asset is None and leftover.thumbnail_saved is not None:
            parts.append(f"saved {_when(leftover.thumbnail_saved, tz)}")
    return ", ".join(parts)


# Helpers -----------------------------------------------------------------------------------------------


def _resolve(name: str | None, ctx: Context) -> Path:
    """Turn what the user typed (a path, a folder name, a device name, or nothing) into a backup folder."""
    if name is not None:
        path = Path(name).expanduser()
        if path.is_dir():
            return path
    if not ctx.dirs:
        raise BackupError(NO_STANDARD_PLACE if name is None else f"No folder at {name}. {NO_STANDARD_PLACE}")
    search = find_backups(ctx.dirs)
    if name is None:
        matches = search.backups
    else:
        wanted = name.casefold()
        matches = [
            backup
            for backup in search.backups
            if backup.path.name.casefold().startswith(wanted) or (backup.device_name or "").casefold() == wanted
        ]
    if len(matches) == 1:
        return matches[0].path
    where = _join(search.searched)
    if not matches and name is None:
        raise BackupError(f"No backups found in {where}. {HOW_TO_BACK_UP}")
    if not matches:
        raise BackupError(f"No folder or backup called {name!r}. Looked in {where}.")
    choices = "; ".join(f"{b.path.name} ({b.name}, {_when(b.last_backup, ctx.tz)})" for b in matches)
    raise BackupError(f"{len(matches)} backups fit. Name one by its folder: {choices}")


def _warn_if_unfinished(info: BackupInfo, ctx: Context) -> None:
    if info.finished is False:
        print("Warning: this backup didn't finish, so some files may be missing from it.", file=ctx.out)


def _flags(backup: BackupInfo) -> list[str]:
    flags = []
    if backup.encrypted:
        flags.append("encrypted")
    if backup.finished is False:
        flags.append("unfinished")
    return flags


def _when(moment: datetime | None, tz: tzinfo | None) -> str:
    return "date unknown" if moment is None else moment.astimezone(tz).strftime("%a %d %b %Y %H:%M %Z")


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count:,} {one if count == 1 else (many or one + 's')}"


def _size(count: int) -> str:
    if count < 1000:
        return _plural(count, "byte")
    value = float(count)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1000
        if value < 1000 or unit == "TB":
            break
    return f"{value:.1f} {unit}"


def _join(paths: Sequence[Path]) -> str:
    return " and ".join(str(p) for p in paths) or "no folders"


def _table(rows: list[list[str]], right: set[int] | frozenset[int] = frozenset()) -> list[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return [
        "  ".join(
            cell.rjust(w) if i in right else cell.ljust(w) for i, (cell, w) in enumerate(zip(row, widths, strict=True))
        ).rstrip()
        for row in rows
    ]
