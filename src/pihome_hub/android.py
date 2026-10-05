"""The Android app, kept on the hub for phones that join by invitation.

Somebody given an invitation scans its code with the phone's camera and lands on
the web client's join page. When the app is not installed yet, the page offers it,
downloaded from this hub: on the home network, with no store, no internet and no
third party in between. The file is the release the operator chose to install with
``pihome-hub-admin app install``, and nothing is offered until there is one.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Final

#: What Android expects an APK to be served as.
APK_MEDIA_TYPE: Final = "application/vnd.android.package-archive"

#: Every APK holds both: the manifest that describes the app, and its code.
_REQUIRED_ENTRIES: Final = ("AndroidManifest.xml", "classes.dex")


class NotAnApkError(Exception):
    """A file that is not an Android package, refused before it replaces one that is."""


@dataclass(frozen=True)
class AppFile:
    """The APK this hub offers, as a phone can check it."""

    sha256: str
    size: int


def check_apk(path: Path) -> None:
    """Refuse anything that is not an APK, so a wrong file is never offered to a phone.

    An APK is a zip holding a manifest and code. That is checked, and nothing more:
    whether it is signed, and by whom, is Android's to judge when it is installed.
    """
    if not path.is_file():
        msg = f"{path} is not a file"
        raise NotAnApkError(msg)
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile as exc:
        msg = f"{path} is not an APK: it is not even a zip archive"
        raise NotAnApkError(msg) from exc
    missing = [entry for entry in _REQUIRED_ENTRIES if entry not in names]
    if missing:
        msg = f"{path} is not an APK: it holds no {', '.join(missing)}"
        raise NotAnApkError(msg)


def install_apk(source: Path, target: Path) -> AppFile:
    """Put ``source`` where the hub serves the app from, replacing what was there.

    Written beside the target and renamed over it, so a phone downloading while this
    runs gets the old file or the new one, never half of one.
    """
    check_apk(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.partial")
    try:
        with source.open("rb") as reading, partial.open("wb") as writing:
            shutil.copyfileobj(reading, writing)
            writing.flush()
            os.fsync(writing.fileno())
        partial.chmod(0o644)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return _describe(target)


def _describe(path: Path) -> AppFile:
    digest = hashlib.sha256()
    with path.open("rb") as reading:
        for chunk in iter(lambda: reading.read(1 << 20), b""):
            digest.update(chunk)
    return AppFile(sha256=digest.hexdigest(), size=path.stat().st_size)


class AndroidApp:
    """The APK at ``path``, if there is one, for the routes that offer it.

    Its checksum is worked out once per version of the file, rather than on every
    request: a small board takes a noticeable moment to hash a few megabytes.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()
        self._known: tuple[tuple[int, int, int], AppFile] | None = None

    def describe(self) -> AppFile | None:
        """The file as it is now, or None while the hub has no app to offer."""
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        if not self.path.is_file():
            return None
        version = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        with self._lock:
            if self._known is None or self._known[0] != version:
                self._known = (version, _describe(self.path))
            return self._known[1]
