"""The Exif we write, read back by Pillow: an independent reader, as a photo app would."""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest
from conftest import JPEG
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

from afterimage.exif import add_exif, has_exif, is_jpeg

DESCRIPTION = "Thumbnail recovered from an iPhone backup by afterimage"
TAKEN = datetime(2026, 10, 18, 13, 2, 9, tzinfo=timezone(timedelta(hours=-5)))  # 18:02:09 UTC


def picture(**save) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (48, 36), (200, 90, 40)).save(buffer, "JPEG", **save)
    return buffer.getvalue()


PICTURE = picture()


def read(jpeg: bytes):
    image = Image.open(io.BytesIO(jpeg))
    image.load()  # the picture itself still decodes
    exif = image.getexif()
    return image, exif, exif.get_ifd(0x8769), exif.get_ifd(0x8825)


def degrees(values, ref: str) -> float:
    total = sum(float(v) / unit for v, unit in zip(values, (1, 60, 3600), strict=True))
    return -total if ref in ("S", "W") else total


def test_date_and_place_read_back():
    image, main, exif, gps = read(add_exif(PICTURE, taken=TAKEN, place=(49.8951, -97.1384), description=DESCRIPTION))

    assert image.size == (48, 36)
    assert main[0x010E] == DESCRIPTION
    assert (exif[0x9003], exif[0x9011], exif[0x9000]) == ("2026:10:18 18:02:09", "+00:00", b"0232")
    assert (gps[1], gps[3]) == ("N", "W")
    assert degrees(gps[2], gps[1]) == pytest.approx(49.8951, abs=1e-6)
    assert degrees(gps[4], gps[3]) == pytest.approx(-97.1384, abs=1e-6)


def test_only_a_date():
    _, _, exif, gps = read(add_exif(PICTURE, taken=TAKEN, place=None, description=DESCRIPTION))
    assert exif[0x9003] == "2026:10:18 18:02:09" and gps == {}


def test_only_a_place():
    _, _, exif, gps = read(add_exif(PICTURE, taken=None, place=(-33.8568, 151.2153), description=DESCRIPTION))
    assert 0x9003 not in exif
    assert (gps[1], gps[3]) == ("S", "E")
    assert degrees(gps[2], gps[1]) == pytest.approx(-33.8568, abs=1e-6)


@settings(max_examples=150, deadline=None)
@given(
    latitude=st.floats(min_value=-90, max_value=90, allow_nan=False),
    longitude=st.floats(min_value=-180, max_value=180, allow_nan=False),
)
def test_any_place_reads_back_to_within_three_centimetres(latitude, longitude):
    _, _, _, gps = read(add_exif(PICTURE, taken=None, place=(latitude, longitude), description=DESCRIPTION))
    assert degrees(gps[2], gps[1]) == pytest.approx(latitude, abs=3e-7)
    assert degrees(gps[4], gps[3]) == pytest.approx(longitude, abs=3e-7)


def test_nothing_to_write_or_nowhere_to_write_it():
    assert add_exif(PICTURE, taken=None, place=None, description=DESCRIPTION) is None
    assert add_exif(b"\x89PNG\r\n\x1a\n", taken=TAKEN, place=None, description=DESCRIPTION) is None


def test_a_picture_that_already_has_exif_is_left_alone():
    existing = Image.Exif()
    existing[0x0112] = 6  # "rotate when shown": exactly the kind of thing that mustn't be lost
    tagged = picture(exif=existing.tobytes())
    assert has_exif(tagged) and not has_exif(PICTURE)
    assert add_exif(tagged, taken=TAKEN, place=None, description=DESCRIPTION) is None


def test_the_jfif_header_stays_first():
    out = add_exif(PICTURE, taken=TAKEN, place=None, description=DESCRIPTION)
    jfif_end = 4 + int.from_bytes(out[4:6], "big")
    assert out[2:4] == b"\xff\xe0" and out[jfif_end : jfif_end + 2] == b"\xff\xe1"


def test_without_a_jfif_header_exif_goes_right_after_the_start():
    jfif_end = 4 + int.from_bytes(PICTURE[4:6], "big")
    bare = PICTURE[:2] + PICTURE[jfif_end:]
    out = add_exif(bare, taken=TAKEN, place=None, description=DESCRIPTION)
    assert out[2:4] == b"\xff\xe1"
    assert read(out)[2][0x9003] == "2026:10:18 18:02:09"


def test_exif_goes_before_other_segments():
    jfif_end = 4 + int.from_bytes(PICTURE[4:6], "big")
    colour_profile = b"\xff\xe2\x00\x10ICC_PROFILE\x00\x01\x01"
    out = add_exif(PICTURE[:2] + colour_profile + PICTURE[jfif_end:], taken=TAKEN, place=None, description=DESCRIPTION)
    assert out[2:4] == b"\xff\xe1" and colour_profile in out
    assert read(out)[2][0x9003] == "2026:10:18 18:02:09"


@pytest.mark.parametrize(
    "jpeg",
    [
        JPEG,  # the tests' stand-in: a header and no picture
        b"\xff\xd8\xff\xe0\x00\x01",  # a segment shorter than its own length field
        b"\xff\xd8\xff\xe1\xff\xff" + b"Exif",  # a segment longer than the file
    ],
)
def test_odd_jpegs_dont_break_it(jpeg):
    assert is_jpeg(jpeg) and not has_exif(jpeg)
    out = add_exif(jpeg, taken=TAKEN, place=None, description=DESCRIPTION)
    assert out is not None and out.endswith(jpeg[-2:]) and has_exif(out)


def test_has_exif_on_something_that_isnt_a_jpeg():
    assert not has_exif(b"GIF89a") and not is_jpeg(b"GIF89a")
