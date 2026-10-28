"""Decode the small NSKeyedArchiver plists that a backup keeps for each file.

Manifest.db stores each file's details (size, dates, permissions) as a binary plist written by Apple's
NSKeyedArchiver. That format is a flat list of objects that point at each other by index ("UIDs"), with
object 0 standing for nil. `unarchive` follows the pointers and hands back plain Python values.
"""

from __future__ import annotations

import plistlib
from typing import Any

MAX_DEPTH = 32  # MBFile records are two or three levels deep; anything near this is broken or a loop


class ArchiveError(ValueError):
    """The bytes aren't an NSKeyedArchiver plist that can be read."""


def unarchive(data: bytes) -> Any:
    """Turn an NSKeyedArchiver plist into Python values: dicts, lists, strings, numbers and bytes.

    An archived object becomes a dict of its fields, plus "$class" holding its class name (e.g. "MBFile").
    """
    try:
        plist = plistlib.loads(data)
    except Exception as error:  # plistlib raises several kinds of error for bad input
        raise ArchiveError("Not a readable plist") from error
    if not isinstance(plist, dict) or plist.get("$archiver") != "NSKeyedArchiver":
        raise ArchiveError("Not an NSKeyedArchiver plist")
    objects, top = plist.get("$objects"), plist.get("$top")
    if not isinstance(objects, list) or not isinstance(top, dict) or "root" not in top:
        raise ArchiveError("The archive has no root object")
    return _resolve(top["root"], objects, 0)


def _resolve(value: Any, objects: list[Any], depth: int) -> Any:
    if depth > MAX_DEPTH:
        raise ArchiveError("The archive nests too deeply, or loops back on itself")
    if isinstance(value, plistlib.UID):
        if value.data >= len(objects):
            raise ArchiveError(f"The archive points at object {value.data}, which doesn't exist")
        value = objects[value.data]
        if value == "$null":
            return None
    if isinstance(value, list):
        return [_resolve(item, objects, depth + 1) for item in value]
    if not isinstance(value, dict):
        return value

    # Foundation's own types are stored in a fixed shape; give back the plain value.
    if "NS.data" in value:
        return _resolve(value["NS.data"], objects, depth + 1)
    if "NS.string" in value:
        return _resolve(value["NS.string"], objects, depth + 1)
    if "NS.keys" in value and "NS.objects" in value:
        keys = _resolve(value["NS.keys"], objects, depth + 1)
        items = _resolve(value["NS.objects"], objects, depth + 1)
        try:
            return dict(zip(keys, items, strict=True))
        except (TypeError, ValueError) as error:
            raise ArchiveError("The archive has a dictionary that can't be rebuilt") from error
    if "NS.objects" in value:
        return _resolve(value["NS.objects"], objects, depth + 1)

    fields = {key: _resolve(item, objects, depth + 1) for key, item in value.items() if key != "$class"}
    if "$class" in value:
        described = _resolve(value["$class"], objects, depth + 1)
        if isinstance(described, dict) and isinstance(described.get("$classname"), str):
            fields["$class"] = described["$classname"]
    return fields
