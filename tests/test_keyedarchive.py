from __future__ import annotations

import plistlib

import pytest
from conftest import mbfile
from hypothesis import given, settings
from hypothesis import strategies as st

from afterimage.keyedarchive import ArchiveError, unarchive

UID = plistlib.UID


def archive(objects: list, root: int = 1) -> bytes:
    plist = {"$archiver": "NSKeyedArchiver", "$version": 100000, "$top": {"root": UID(root)}, "$objects": objects}
    return plistlib.dumps(plist, fmt=plistlib.FMT_BINARY)


def test_reads_a_file_record():
    record = unarchive(mbfile("Media/DCIM/100APPLE/IMG_0001.HEIC", 2_345_678, 1_760_000_000))

    assert record["$class"] == "MBFile"
    assert (record["RelativePath"], record["Size"], record["LastModified"], record["Mode"]) == (
        "Media/DCIM/100APPLE/IMG_0001.HEIC",
        2_345_678,
        1_760_000_000,
        0o100644,
    )


def test_foundation_types_become_plain_values():
    objects = [
        "$null",
        {"$class": UID(8), "Data": UID(2), "Name": UID(3), "Attributes": UID(4), "Tags": UID(7), "Nothing": UID(0)},
        {"NS.data": b"\x01\x02"},
        {"NS.string": "hello"},
        {"NS.keys": [UID(5)], "NS.objects": [UID(6)]},
        "com.apple.assetsd.UUID",
        b"abc",
        {"NS.objects": [UID(3), UID(5)]},
        {"$classname": "Thing", "$classes": ["Thing", "NSObject"]},
    ]

    assert unarchive(archive(objects)) == {
        "$class": "Thing",
        "Data": b"\x01\x02",
        "Name": "hello",
        "Attributes": {"com.apple.assetsd.UUID": b"abc"},
        "Tags": ["hello", "com.apple.assetsd.UUID"],
        "Nothing": None,
    }


def test_a_class_without_a_name_is_left_out():
    assert unarchive(archive(["$null", {"$class": UID(2), "Size": 3}, "not a class"])) == {"Size": 3}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"not a plist", "Not a readable plist"),
        (plistlib.dumps({"a": 1}), "Not an NSKeyedArchiver plist"),
        (plistlib.dumps([1, 2]), "Not an NSKeyedArchiver plist"),
        (plistlib.dumps({"$archiver": "NSKeyedArchiver", "$objects": []}), "no root object"),
        (archive(["$null"], root=5), "object 5, which doesn't exist"),
        (archive(["$null", {"Me": UID(1)}]), "loops back on itself"),
        (
            archive(["$null", {"NS.keys": [UID(2)], "NS.objects": [UID(3)]}, {"NS.objects": []}, "x"]),
            "can't be rebuilt",
        ),
        (archive(["$null", {"NS.keys": [UID(2), UID(2)], "NS.objects": [UID(2)]}, "x"]), "can't be rebuilt"),
    ],
)
def test_broken_archives_are_refused(data, message):
    with pytest.raises(ArchiveError, match=message):
        unarchive(data)


@settings(max_examples=200)
@given(
    path=st.text(min_size=1, max_size=200),
    size=st.integers(min_value=0, max_value=2**63 - 1),
    modified=st.integers(min_value=-(2**31), max_value=2**33),
)
def test_any_file_record_reads_back(path, size, modified):
    """Whatever the path (any language, any symbol) and however big or old the file, the record reads back."""
    record = unarchive(mbfile(path, size, modified))
    assert (record["RelativePath"], record["Size"], record["LastModified"]) == (path, size, modified)
