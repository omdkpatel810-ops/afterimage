"""Encrypted backups: unlocking with the password, decrypting, and refusing what doesn't add up."""

from __future__ import annotations

import io
import os
import plistlib
import struct
import sys
from datetime import timezone
from pathlib import Path

import pytest
from conftest import (
    CAMERA_ROLL,
    HEIC,
    MOV,
    PASSWORD,
    Encryption,
    FakeBackup,
    add_camera_roll,
    encrypt,
    snapshot,
)

from afterimage import crypto
from afterimage.backup import Backup, BackupError, PasswordNeeded
from afterimage.camera_roll import scan
from afterimage.cli import main
from afterimage.library import read_library


def locked(backups_dir: Path, **options) -> FakeBackup:
    fake = add_camera_roll(FakeBackup(backups_dir))
    fake.encryption = Encryption(PASSWORD, **options)
    return fake


@pytest.fixture
def vault(backups_dir):
    """The usual camera roll, in a backup encrypted with PASSWORD."""
    return locked(backups_dir).build()


def run(*args, dirs, password=PASSWORD, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    code = main(
        list(args), out=out, err=err, backup_dirs=dirs, tz=timezone.utc, ask_password=lambda info: password, stdin=stdin
    )
    return code, out.getvalue(), err.getvalue()


def test_the_files_really_are_encrypted(vault):
    stored = [p.read_bytes() for p in vault.glob("??/*")]
    assert stored and MOV not in stored and HEIC not in stored
    assert not (vault / "Manifest.db").read_bytes().startswith(b"SQLite format 3")


def test_opening_needs_the_password(vault):
    with pytest.raises(PasswordNeeded, match="needs its backup password"):
        Backup(vault)


def test_the_password_opens_everything(vault):
    with Backup(vault, PASSWORD) as backup:
        assert backup.encrypted
        files = {f.relative_path: f for f in backup.files(CAMERA_ROLL)}
        assert backup.read(files["Media/DCIM/100APPLE/IMG_0003.MOV"]) == MOV
        roll = scan(backup)
        library = read_library(backup, roll)  # Photos.sqlite and its log, decrypted then read
        scratch = Path(backup._scratch.name)
        assert (scratch / "Manifest.db").read_bytes().startswith(b"SQLite format 3")
    assert len(library.assets) == 5 and roll.not_on_disk == []
    assert not scratch.exists()  # the decrypted list of files is gone once the backup is closed


def test_encrypted_and_plain_backups_tell_the_same_story(backups_dir, tmp_path):
    plain = add_camera_roll(FakeBackup(tmp_path / "plain")).build()
    encrypted = locked(backups_dir).build()

    _, expected, _ = run("photos", str(plain), "--list", dirs=[])
    code, out, err = run("photos", str(encrypted), "--list", dirs=[])

    assert code == 0 and out == expected
    assert err.strip() == "Unlocking the backup. This takes a few seconds."


def test_export_from_an_encrypted_backup(vault, tmp_path):
    before = snapshot(vault)
    code, out, _ = run("export", str(vault), "--out", str(tmp_path / "saved"), dirs=[])
    assert code == 0 and out.startswith("Saved 4 files")
    assert (tmp_path / "saved/recently-deleted-full-size/IMG_0003.MOV").read_bytes() == MOV
    assert snapshot(vault) == before


def test_the_password_can_come_from_standard_input(vault):
    code, out, _ = run(
        "photos", str(vault), "--password-stdin", dirs=[], password=None, stdin=io.StringIO(PASSWORD + "\n")
    )
    assert code == 0 and "5 photos and videos" in out


def test_a_wrong_password(vault):
    code, _, err = run("photos", str(vault), dirs=[], password="hunter2")
    assert code == 1
    assert "That password doesn't unlock the backup of Om's iPhone." in err
    assert "not the phone's passcode" in err


def test_no_password(vault):
    code, _, err = run("photos", str(vault), dirs=[], password="")
    assert (code, err.strip()) == (1, "error: The backup of Om's iPhone is encrypted, and no password was given.")


def test_stopping_at_the_password_prompt(vault):
    def interrupted(info):
        raise KeyboardInterrupt

    out, err = io.StringIO(), io.StringIO()
    assert main(["photos", str(vault)], out=out, err=err, backup_dirs=[], ask_password=interrupted) == 130


def test_older_keybags_without_the_sha256_round(backups_dir):
    with Backup(locked(backups_dir, dpic=None).build(), PASSWORD) as backup:
        assert len(list(backup.files())) == 22


def test_before_ios_10_2_the_list_of_files_was_not_encrypted(backups_dir):
    root = locked(backups_dir, manifest_key=False).build()
    assert (root / "Manifest.db").read_bytes().startswith(b"SQLite format 3")
    with Backup(root, PASSWORD) as backup:
        movie = next(f for f in backup.files() if f.name == "IMG_0003.MOV")
        assert backup.read(movie) == MOV


def test_without_cryptography_it_says_what_to_install(vault, monkeypatch):
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives", None)  # as if it were not installed
    with pytest.raises(BackupError, match=r'pip install "afterimage\[encrypted\]"'):
        Backup(vault, PASSWORD)


# Tampered or damaged backups ----------------------------------------------------------------------------


def rewrite_manifest_plist(root: Path, change) -> None:
    plist = plistlib.loads((root / "Manifest.plist").read_bytes())
    change(plist)
    (root / "Manifest.plist").write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_BINARY))


@pytest.mark.parametrize(("tag", "value"), [(b"DPIC", 10**9), (b"ITER", 0)])
def test_tampered_key_settings_are_refused_at_once(vault, tag, value):
    """A billion rounds would keep the computer busy for hours, so it's refused rather than tried."""

    def change(plist):
        entries = crypto.tlv(plist["BackupKeyBag"])
        plist["BackupKeyBag"] = b"".join(_tlv(t, struct.pack(">I", value) if t == tag else v) for t, v in entries)

    rewrite_manifest_plist(vault, change)
    with pytest.raises(BackupError, match="password settings look wrong"):
        Backup(vault, PASSWORD)


def test_keys_only_the_phone_itself_can_open(backups_dir):
    with pytest.raises(BackupError, match="no keys a password can open"):
        Backup(locked(backups_dir, device_only=True).build(), PASSWORD)


def test_an_encrypted_backup_without_a_keybag(vault):
    rewrite_manifest_plist(vault, lambda plist: plist.pop("BackupKeyBag"))
    with pytest.raises(BackupError, match="has no keybag"):
        Backup(vault, PASSWORD)


def test_a_damaged_list_of_files(vault):
    (vault / "Manifest.db").write_bytes((vault / "Manifest.db").read_bytes()[:-1])
    with pytest.raises(BackupError, match="isn't a whole number of AES blocks"):
        Backup(vault, PASSWORD)


def test_a_damaged_file(vault, tmp_path):
    with Backup(vault, PASSWORD) as backup:
        movie = next(f for f in backup.files() if f.name == "IMG_0003.MOV")
        stored = backup.locate(movie)
        stored.write_bytes(stored.read_bytes()[:-16] + bytes(16))  # the padding no longer adds up
        with pytest.raises(BackupError, match="Couldn't decrypt Media/DCIM/100APPLE/IMG_0003.MOV"):
            backup.read(movie)
        with pytest.raises(BackupError, match="doesn't end the way it should"):
            backup.copy(movie, tmp_path / "copy.mov")
        assert not (tmp_path / "copy.mov").exists()


def test_damaged_files_are_left_out_of_an_export(vault, tmp_path):
    with Backup(vault, PASSWORD) as backup:
        for entry in backup.files(CAMERA_ROLL):
            if entry.name in ("IMG_0003.MOV", "5005.JPG", "5003.JPG"):
                stored = backup.locate(entry)
                stored.write_bytes(stored.read_bytes()[:-16] + bytes(16))
    _, out, _ = run("export", str(vault), "--out", str(tmp_path / "saved"), dirs=[])
    assert out.startswith("Saved 0 files")
    assert "4 photos and videos couldn't be saved" in out


def test_a_damaged_photos_database(vault):
    with Backup(vault, PASSWORD) as backup:
        database = next(f for f in backup.files() if f.name == "Photos.sqlite")
        stored = backup.locate(database)
        stored.write_bytes(stored.read_bytes()[:-1])
    _, out, _ = run("photos", str(vault), dirs=[])
    assert "  Photos database    unreadable" in out
    assert "The Photos database couldn't be read. Couldn't decrypt Media/PhotoData/Photos.sqlite" in out


def test_a_file_without_a_key(backups_dir):
    fake = locked(backups_dir)
    fake.file(CAMERA_ROLL, "Media/DCIM/100APPLE/IMG_0099.JPG", b"x", blob=b"no record")
    with Backup(fake.build(), PASSWORD) as backup:
        keyless = next(f for f in backup.files() if f.name == "IMG_0099.JPG")
        with pytest.raises(BackupError, match="the backup holds no key for it"):
            backup.read(keyless)


def test_empty_files_stay_empty(backups_dir, tmp_path):
    fake = locked(backups_dir)
    fake.file(CAMERA_ROLL, "Media/empty.txt", b"", blob=None)
    with Backup(fake.build(), PASSWORD) as backup:
        empty = next(f for f in backup.files() if f.name == "empty.txt")
        backup.locate(empty).write_bytes(b"")  # some backups store empty files as nothing at all
        assert backup.read(empty) == b""
        backup.copy(empty, tmp_path / "empty.txt")
    assert (tmp_path / "empty.txt").read_bytes() == b""


# The pieces on their own --------------------------------------------------------------------------------


def test_big_files_are_decrypted_a_piece_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "CHUNK", 64)
    key, data = os.urandom(32), os.urandom(1000)
    (tmp_path / "secret").write_bytes(encrypt(key, data))
    crypto.decrypt_file(key, tmp_path / "secret", tmp_path / "plain")
    assert (tmp_path / "plain").read_bytes() == data == crypto.decrypt(key, encrypt(key, data))


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: crypto.decrypt(bytes(32), b"short"), "whole number of AES blocks"),
        (lambda: crypto.decrypt(bytes(5), bytes(16)), "file key in the backup has the wrong length"),
        (lambda: crypto.class_unwrap({3: bytes(32)}, 9, bytes(40)), "no key for protection class 9"),
        (lambda: crypto.class_unwrap({3: bytes(32)}, 3, bytes(24)), "wrapped key in the backup has the wrong length"),
        (lambda: crypto.class_unwrap({3: bytes(32)}, 3, bytes(40)), "didn't unwrap"),
        (lambda: crypto.parse_keybag(_tlv(b"CLAS", struct.pack(">I", 3))), "isn't laid out as expected"),
        (lambda: crypto.password_key(crypto.Keybag({b"ITER": 10, b"SALT": b""}), "x"), "settings look wrong"),
    ],
)
def test_what_gets_refused(call, message):
    with pytest.raises(crypto.CryptoError, match=message):
        call()


def _tlv(tag: bytes, value: bytes) -> bytes:
    return tag + struct.pack(">I", len(value)) + value


def test_matches_an_independent_implementation(vault):
    """iphone_backup_decrypt, an established open-source decrypter, reads our sample backups the same way."""
    reference = pytest.importorskip("iphone_backup_decrypt")
    theirs = reference.EncryptedBackup(backup_directory=str(vault), passphrase=PASSWORD)
    with Backup(vault, PASSWORD) as backup:
        for entry in backup.files(CAMERA_ROLL):
            if entry.kind == "file":
                assert theirs.extract_file_as_bytes(entry.relative_path, domain_like=CAMERA_ROLL) == backup.read(entry)


def test_nothing_decrypts_to_nothing():
    assert crypto.decrypt(bytes(32), b"") == b""


def test_reading_a_file_missing_from_the_folder(backups_dir):
    fake = FakeBackup(backups_dir)
    fake.file(CAMERA_ROLL, "Media/DCIM/100APPLE/IMG_0001.JPG", b"x", on_disk=False)
    with Backup(fake.build()) as backup:
        (entry,) = backup.files()
        with pytest.raises(BackupError, match="is listed in the backup, but its file is missing"):
            backup.read(entry)
