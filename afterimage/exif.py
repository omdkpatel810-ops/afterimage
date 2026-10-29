"""Write the date a photo was taken and where into a JPEG, as Exif, using only the standard library.

Photo apps read a picture's date and place from its Exif block: a small TIFF structure stored in an APP1
segment near the start of the JPEG. The thumbnails in a backup usually carry neither, so a saved thumbnail
would land on the day it was saved, with no place. This adds:

- in the main directory: a description saying where the picture came from;
- in the Exif directory: DateTimeOriginal, with OffsetTimeOriginal "+00:00" because the time is in UTC;
- in the GPS directory: latitude and longitude as degrees, minutes and seconds.

A JPEG that already has Exif is left alone, so nothing it holds (such as which way up it goes) is lost.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from datetime import datetime, timezone

BYTE, ASCII, LONG, RATIONAL, UNDEFINED = 1, 2, 4, 5, 7
IMAGE_DESCRIPTION, EXIF_IFD, GPS_IFD = 0x010E, 0x8769, 0x8825
EXIF_VERSION, DATE_TIME_ORIGINAL, OFFSET_TIME_ORIGINAL = 0x9000, 0x9003, 0x9011
GPS_VERSION, GPS_LATITUDE_REF, GPS_LATITUDE, GPS_LONGITUDE_REF, GPS_LONGITUDE = 0, 1, 2, 3, 4

SOI = b"\xff\xd8"
EXIF_HEADER = b"Exif\x00\x00"

Entry = tuple[int, int, bytes]  # type, count, value


def is_jpeg(data: bytes) -> bool:
    return data.startswith(SOI + b"\xff")


def has_exif(jpeg: bytes) -> bool:
    return any(
        marker == 0xE1 and jpeg[start + 4 : end].startswith(EXIF_HEADER) for marker, start, end in _segments(jpeg)
    )


def add_exif(
    jpeg: bytes, *, taken: datetime | None, place: tuple[float, float] | None, description: str
) -> bytes | None:
    """The JPEG with its date taken and place written in, or None to keep it as it is.

    None means there's nothing to write, it isn't a JPEG, or it already has Exif.
    """
    if (taken is None and place is None) or not is_jpeg(jpeg) or has_exif(jpeg):
        return None
    payload = EXIF_HEADER + tiff(taken, place, description)
    segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    # Keep a JFIF header (APP0) first, where readers expect it; otherwise go straight after the start marker.
    at = len(SOI)
    for marker, _, end in _segments(jpeg):
        if marker != 0xE0:
            break
        at = end
    return jpeg[:at] + segment + jpeg[at:]


def tiff(taken: datetime | None, place: tuple[float, float] | None, description: str) -> bytes:
    """The Exif data itself: a big-endian TIFF header and its directories."""
    main: dict[int, Entry] = {IMAGE_DESCRIPTION: _ascii(description), EXIF_IFD: _long(0)}
    exif: dict[int, Entry] = {EXIF_VERSION: (UNDEFINED, 4, b"0232")}
    if taken is not None:
        exif[DATE_TIME_ORIGINAL] = _ascii(taken.astimezone(timezone.utc).strftime("%Y:%m:%d %H:%M:%S"))
        exif[OFFSET_TIME_ORIGINAL] = _ascii("+00:00")
    gps: dict[int, Entry] = {}
    if place is not None:
        latitude, longitude = place
        main[GPS_IFD] = _long(0)
        gps = {
            GPS_VERSION: (BYTE, 4, bytes([2, 3, 0, 0])),
            GPS_LATITUDE_REF: _ascii("N" if latitude >= 0 else "S"),
            GPS_LATITUDE: _rationals(_degrees(latitude)),
            GPS_LONGITUDE_REF: _ascii("E" if longitude >= 0 else "W"),
            GPS_LONGITUDE: _rationals(_degrees(longitude)),
        }

    # Each directory's size doesn't depend on where the others are, so lay them out end to end,
    # then fill in the pointers from the main directory.
    exif_at = 8 + len(_directory(main, 0))
    gps_at = exif_at + len(_directory(exif, exif_at))
    main[EXIF_IFD] = _long(exif_at)
    if gps:
        main[GPS_IFD] = _long(gps_at)
    blocks = [_directory(main, 8), _directory(exif, exif_at)] + ([_directory(gps, gps_at)] if gps else [])
    return b"MM\x00\x2a" + struct.pack(">I", 8) + b"".join(blocks)


def _directory(entries: dict[int, Entry], start: int) -> bytes:
    """One TIFF directory starting at `start`: its entries, then the values too big to fit in them."""
    values_at = start + 2 + 12 * len(entries) + 4
    head, values = struct.pack(">H", len(entries)), b""
    for tag in sorted(entries):
        kind, count, value = entries[tag]
        if len(value) <= 4:
            head += struct.pack(">HHI", tag, kind, count) + value.ljust(4, b"\x00")
        else:
            head += struct.pack(">HHII", tag, kind, count, values_at + len(values))
            values += value + (b"\x00" if len(value) % 2 else b"")  # values start on even offsets
    return head + struct.pack(">I", 0) + values


def _ascii(text: str) -> Entry:
    value = text.encode("ascii", "replace") + b"\x00"
    return ASCII, len(value), value


def _long(number: int) -> Entry:
    return LONG, 1, struct.pack(">I", number)


def _rationals(pairs: list[tuple[int, int]]) -> Entry:
    return RATIONAL, len(pairs), b"".join(struct.pack(">II", top, bottom) for top, bottom in pairs)


def _degrees(value: float) -> list[tuple[int, int]]:
    """Degrees, minutes and seconds (to a thousandth of a second, about 3 cm), counted in whole numbers."""
    thousandths = round(abs(value) * 3_600_000)
    degrees, rest = divmod(thousandths, 3_600_000)
    minutes, seconds = divmod(rest, 60_000)
    return [(degrees, 1), (minutes, 1), (seconds, 1000)]


def _segments(jpeg: bytes) -> Iterator[tuple[int, int, int]]:
    """The application segments (APP0 to APP15) after the start marker: (marker, start, end)."""
    if not jpeg.startswith(SOI):
        return
    at = len(SOI)
    while at + 4 <= len(jpeg) and jpeg[at] == 0xFF and 0xE0 <= jpeg[at + 1] <= 0xEF:
        end = at + 2 + int.from_bytes(jpeg[at + 2 : at + 4], "big")
        if end < at + 4 or end > len(jpeg):
            return
        yield jpeg[at + 1], at, end
        at = end
