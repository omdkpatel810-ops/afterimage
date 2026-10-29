"""Unlock an encrypted backup with its password, and decrypt its files.

How an encrypted backup is protected (iOS 10.2 and later):

1. Manifest.plist holds a keybag: a list of "class keys", each wrapped (AES key wrap, RFC 3394) by a key made
   from the backup password. That key takes two rounds of PBKDF2: HMAC-SHA256 with the salt DPSL for DPIC
   rounds (about ten million, which is why unlocking takes a few seconds), then HMAC-SHA1 with SALT for ITER.
2. Manifest.db is encrypted (AES-256-CBC, zero IV) with a key wrapped by one of the class keys: ManifestKey.
3. Every file is encrypted the same way with its own key, kept wrapped in its Manifest.db record.

The password is used once, to make the key, and isn't kept. PBKDF2 comes from the standard library; AES needs
the `cryptography` package, installed with: pip install "afterimage[encrypted]".
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WRAP_PASSCODE = 2  # a class key wrapped with the password (1 would be the phone's own hardware key)
MAX_DPIC = 20_000_000  # bounds on the key-making rounds, so a tampered backup can't keep us busy forever
MAX_ITER = 1_000_000
CHUNK = 1 << 20
_CLASS_TAGS = (b"CLAS", b"WRAP", b"WPKY", b"KTYP", b"PBKY")


class CryptoError(ValueError):
    """The backup can't be decrypted; the message says why, in plain words."""


class WrongPassword(CryptoError):
    pass


class CryptoUnavailable(CryptoError):
    pass


@dataclass
class Keybag:
    attributes: dict[bytes, Any] = field(default_factory=dict)  # SALT, ITER, DPSL, DPIC, ...
    classes: dict[int, dict[bytes, Any]] = field(default_factory=dict)  # by protection class


def tlv(blob: bytes) -> Iterator[tuple[bytes, bytes]]:
    """A keybag's entries: a 4-byte tag, a 4-byte big-endian length, then that many bytes."""
    at = 0
    while at + 8 <= len(blob):
        length = struct.unpack(">I", blob[at + 4 : at + 8])[0]
        yield blob[at : at + 4], blob[at + 8 : at + 8 + length]
        at += 8 + length


def parse_keybag(blob: bytes) -> Keybag:
    """Read a keybag. The first UUID and WRAP describe the bag itself; each later UUID starts a class key."""
    keybag, current = Keybag(), None
    seen_uuid = seen_wrap = False
    for tag, data in tlv(blob):
        value: Any = struct.unpack(">I", data)[0] if len(data) == 4 else data
        if tag == b"UUID" and not seen_uuid:
            seen_uuid = True
        elif tag == b"WRAP" and not seen_wrap:
            seen_wrap = True
        elif tag == b"UUID":
            current = {b"UUID": value}
        elif tag in _CLASS_TAGS:
            if current is None:
                raise CryptoError("The backup's keybag isn't laid out as expected.")
            current[tag] = value
            if tag == b"CLAS":
                keybag.classes[value] = current
        else:
            keybag.attributes[tag] = value
    return keybag


def password_key(keybag: Keybag, password: str) -> bytes:
    """The key the backup password stands for."""
    secret = password.encode("utf-8")
    if b"DPSL" in keybag.attributes:  # iOS 10.2 and later add a slow SHA-256 round first
        rounds = _rounds(keybag.attributes.get(b"DPIC"), MAX_DPIC)
        secret = hashlib.pbkdf2_hmac("sha256", secret, _salt(keybag.attributes[b"DPSL"]), rounds, 32)
    rounds = _rounds(keybag.attributes.get(b"ITER"), MAX_ITER)
    return hashlib.pbkdf2_hmac("sha1", secret, _salt(keybag.attributes.get(b"SALT")), rounds, 32)


def unlock(keybag: Keybag, password: str) -> dict[int, bytes]:
    """Every class key the password opens, by protection class. A wrong password opens none."""
    key = password_key(keybag, password)
    keys = {}
    for protection_class, entry in keybag.classes.items():
        wrapped = entry.get(b"WPKY")
        if isinstance(wrapped, bytes) and isinstance(entry.get(b"WRAP"), int) and entry[b"WRAP"] & WRAP_PASSCODE:
            try:
                keys[protection_class] = unwrap(key, wrapped)
            except CryptoUnavailable:
                raise
            except CryptoError as error:
                raise WrongPassword("That password doesn't unlock this backup.") from error
    if not keys:
        raise CryptoError("The backup's keybag has no keys a password can open.")
    return keys


def class_unwrap(keys: dict[int, bytes], protection_class: int, wrapped: bytes) -> bytes:
    """Unwrap a file's (or the manifest's) own key with the key of its protection class."""
    if protection_class not in keys:
        raise CryptoError(f"The backup has no key for protection class {protection_class}.")
    if len(wrapped) != 40:
        raise CryptoError("A wrapped key in the backup has the wrong length.")
    return unwrap(keys[protection_class], wrapped)


def unwrap(key: bytes, wrapped: bytes) -> bytes:
    keywrap, *_ = _aes()
    try:
        return keywrap.aes_key_unwrap(key, wrapped)
    except (keywrap.InvalidUnwrap, ValueError) as error:
        raise CryptoError("A key in the backup didn't unwrap.") from error


def decrypt(key: bytes, data: bytes) -> bytes:
    """Decrypt a whole file held in memory, and take off its padding."""
    if not data:
        return b""
    if len(data) % 16:
        raise CryptoError("An encrypted file's length isn't a whole number of AES blocks.")
    decryptor = _decryptor(key)
    return _unpad(decryptor.update(data) + decryptor.finalize())


def decrypt_file(key: bytes, source: Path, target: Path) -> None:
    """Decrypt a file a megabyte at a time, so even a huge one never has to fit in memory."""
    size = source.stat().st_size
    if size % 16:
        raise CryptoError(f"{source.name} isn't a whole number of AES blocks long.")
    decryptor = _decryptor(key)
    with source.open("rb") as reader, target.open("wb") as writer:
        held = b""  # the last block is kept back until the end, to take its padding off
        while chunk := reader.read(CHUNK):
            plain = held + decryptor.update(chunk)
            held, plain = plain[-16:], plain[:-16]
            writer.write(plain)
        if size:
            writer.write(_unpad(held + decryptor.finalize()))


def _decryptor(key: bytes) -> Any:
    _, cipher, algorithms, modes = _aes()
    if len(key) not in (16, 24, 32):
        raise CryptoError("A file key in the backup has the wrong length.")
    return cipher(algorithms.AES(key), modes.CBC(bytes(16))).decryptor()


def _unpad(data: bytes) -> bytes:
    """Remove PKCS #7 padding: the last byte says how many bytes of padding there are, all equal to it."""
    count = data[-1] if data else 0
    if not 1 <= count <= 16 or data[-count:] != bytes([count]) * count:
        raise CryptoError("A decrypted file doesn't end the way it should. The backup may be damaged.")
    return data[:-count]


def _rounds(value: Any, maximum: int) -> int:
    if not isinstance(value, int) or not 1 <= value <= maximum:
        raise CryptoError("The backup's password settings look wrong, so afterimage won't use them.")
    return value


def _salt(value: Any) -> bytes:
    if not isinstance(value, bytes) or not value:
        raise CryptoError("The backup's password settings look wrong, so afterimage won't use them.")
    return value


def _aes() -> tuple[Any, Any, Any, Any]:
    try:
        from cryptography.hazmat.primitives import keywrap
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as error:
        raise CryptoUnavailable(
            'Opening an encrypted backup needs the cryptography package. Install it with: pip install "afterimage[encrypted]"'
        ) from error
    return keywrap, Cipher, algorithms, modes
