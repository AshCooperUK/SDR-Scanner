"""Resolve finalized scanner recordings for read-only local playback."""

import hashlib
import os
import re
import stat
from contextlib import ExitStack

from recording import CLIP_NAME, DIRECTORY_FLAGS, _frequency_folder, _open_directory
from scanner import MANUAL_MAX_HZ, MANUAL_MIN_HZ


RECORDINGS_URL = "/api/recordings/"
FREQUENCY_NAME = re.compile(r"([0-9]{1,4})\.([0-9]{4,6})mhz")
CLIP_ID = re.compile(r"[0-9a-f]{24}")
BYTE_RANGE = re.compile(r"bytes=([0-9]{0,20})-([0-9]{0,20})", re.IGNORECASE)


def _valid_frequency(name):
    match = FREQUENCY_NAME.fullmatch(name)
    if match is None:
        return False
    whole, fraction = match.groups()
    hz = int(whole) * 1_000_000 + int(fraction.ljust(6, "0"))
    return MANUAL_MIN_HZ <= hz <= MANUAL_MAX_HZ and _frequency_folder(hz) == name


def _finalized(info):
    return stat.S_ISREG(info.st_mode) and info.st_size > 0 and info.st_nlink == 1


def _fingerprint(info):
    identity = f"{info.st_dev}:{info.st_ino}:{info.st_ctime_ns}:{info.st_mtime_ns}:{info.st_size}"
    return hashlib.sha256(identity.encode("ascii")).hexdigest()[:24]


def latest_clip(root):
    """Find the most recently finalized clip without following directory links."""
    try:
        directory = _open_directory(root)
    except FileNotFoundError:
        return None
    newest = None
    try:
        with os.scandir(directory) as folders:
            for folder in folders:
                if not _valid_frequency(folder.name):
                    continue
                try:
                    freq = os.open(folder.name, DIRECTORY_FLAGS, dir_fd=directory)
                except OSError:
                    continue
                try:
                    with os.scandir(freq) as clips:
                        for clip in clips:
                            if not CLIP_NAME.fullmatch(clip.name):
                                continue
                            try:
                                info = clip.stat(follow_symlinks=False)
                            except OSError:
                                continue
                            if _finalized(info):
                                candidate = (info.st_mtime_ns, info.st_ctime_ns, folder.name, clip.name)
                                if newest is None or candidate > newest[0]:
                                    newest = candidate, _fingerprint(info)
                finally:
                    os.close(freq)
    finally:
        os.close(directory)
    if newest is None:
        return None
    (_, _, folder, name), fingerprint = newest
    return {"url": f"{RECORDINGS_URL}{folder}/{name}/{fingerprint}",
            "label": f"{folder.removesuffix('mhz')} MHz · {name}"}


def open_clip(root, route):
    """Open the exact file selected for replay. The caller owns the stream."""
    parts = route.split("/")
    if len(parts) != 3:
        raise FileNotFoundError
    frequency, name, fingerprint = parts
    if not (_valid_frequency(frequency) and CLIP_NAME.fullmatch(name) and CLIP_ID.fullmatch(fingerprint)):
        raise FileNotFoundError
    with ExitStack() as stack:
        directory = _open_directory(root)
        stack.callback(os.close, directory)
        folder = os.open(frequency, DIRECTORY_FLAGS, dir_fd=directory)
        stack.callback(os.close, folder)
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        try:
            info = os.fstat(descriptor)
            if not _finalized(info) or _fingerprint(info) != fingerprint:
                raise FileNotFoundError
            return os.fdopen(descriptor, "rb"), info.st_size
        except BaseException:
            os.close(descriptor)
            raise


def byte_range(header, size):
    """Return inclusive byte bounds for a single HTTP range."""
    match = BYTE_RANGE.fullmatch(header)
    if match is None:
        raise ValueError("Unsupported byte range")
    first, last = match.groups()
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    elif last:
        start, end = max(0, size - int(last)), size - 1
    else:
        raise ValueError("Empty byte range")
    if not 0 <= start <= end < size:
        raise ValueError("Unsatisfiable byte range")
    return start, end
