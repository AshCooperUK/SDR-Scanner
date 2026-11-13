"""Resolve finalized scanner recordings for read-only local playback."""

import hashlib
import os
import re
import stat
from contextlib import ExitStack
from urllib.parse import quote, unquote

from recording import (
    CLIP_NAME, DAB_CLIP_NAME, DIRECTORY_FLAGS, FLAT_CLIP_NAME, FREQUENCY_NAME,
    _frequency_folder, _open_directory, _station_folder,
)
from scanner import MANUAL_MAX_HZ, MANUAL_MIN_HZ


RECORDINGS_URL = "/api/recordings/"
CLIP_ID = re.compile(r"[0-9a-f]{24}")
BYTE_RANGE = re.compile(r"bytes=([0-9]{0,20})-([0-9]{0,20})", re.IGNORECASE)


def _valid_frequency(name):
    match = FREQUENCY_NAME.fullmatch(name)
    if match is None:
        return False
    whole, fraction = match.groups()
    hz = int(whole) * 1_000_000 + int(fraction.ljust(6, "0"))
    return MANUAL_MIN_HZ <= hz <= MANUAL_MAX_HZ and _frequency_folder(hz) == name


def _clip_pattern(folder):
    if _valid_frequency(folder):
        return CLIP_NAME
    if folder and _station_folder(folder) == folder:
        return DAB_CLIP_NAME
    return None


def _flat_clip(name):
    match = FLAT_CLIP_NAME.fullmatch(name)
    return match if match and _clip_pattern(match["prefix"]) is not None else None


def _finalized(info):
    return stat.S_ISREG(info.st_mode) and info.st_size > 0 and info.st_nlink == 1


def _fingerprint(info):
    identity = f"{info.st_dev}:{info.st_ino}:{info.st_ctime_ns}:{info.st_mtime_ns}:{info.st_size}"
    return hashlib.sha256(identity.encode("ascii")).hexdigest()[:24]


def _list_clips(directory):
    with os.scandir(directory) as entries:
        for entry in entries:
            if _flat_clip(entry.name):
                yield "", entry
            if not entry.is_dir(follow_symlinks=False):
                continue
            pattern = _clip_pattern(entry.name)
            if pattern is None:
                continue
            try:
                folder = os.open(entry.name, DIRECTORY_FLAGS, dir_fd=directory)
            except OSError:
                continue
            try:
                with os.scandir(folder) as clips:
                    for clip in clips:
                        if pattern.fullmatch(clip.name):
                            yield entry.name, clip
            finally:
                os.close(folder)


def latest_clip(root):
    """Find the most recently finalized clip without following directory links."""
    try:
        directory = _open_directory(root)
    except FileNotFoundError:
        return None
    newest = None
    try:
        for folder, clip in _list_clips(directory):
            try:
                info = clip.stat(follow_symlinks=False)
            except OSError:
                continue
            if _finalized(info):
                candidate = (info.st_mtime_ns, info.st_ctime_ns, folder, clip.name)
                if newest is None or candidate > newest[0]:
                    newest = candidate, _fingerprint(info)
    finally:
        os.close(directory)
    if newest is None:
        return None
    (_, _, folder, name), fingerprint = newest
    if folder:
        prefix, stamp = folder, name
        route = f"{quote(folder, safe='')}/{name}"
    else:
        match = FLAT_CLIP_NAME.fullmatch(name)
        prefix, stamp = match["prefix"], match["stamp"]
        route = quote(name, safe="")
    label = f"{prefix.removesuffix('mhz')} MHz" if _valid_frequency(prefix) else prefix
    return {"url": f"{RECORDINGS_URL}{route}/{fingerprint}", "label": f"{label} · {stamp}"}


def open_clip(root, route):
    """Open the exact file selected for replay. The caller owns the stream."""
    parts = route.split("/")
    if len(parts) not in (2, 3) or CLIP_ID.fullmatch(parts[-1]) is None:
        raise FileNotFoundError
    encoded_component = parts[0]
    fingerprint = parts[-1]
    try:
        component = unquote(encoded_component, errors="strict")
    except UnicodeError:
        raise FileNotFoundError from None
    if len(parts) == 2:
        folder_name, name = None, component
        if _flat_clip(name) is None:
            raise FileNotFoundError
    else:
        folder_name, name = component, parts[1]
        pattern = _clip_pattern(folder_name)
        if pattern is None or not pattern.fullmatch(name):
            raise FileNotFoundError
    with ExitStack() as stack:
        directory = _open_directory(root)
        stack.callback(os.close, directory)
        if folder_name is not None:
            directory = os.open(folder_name, DIRECTORY_FLAGS, dir_fd=directory)
            stack.callback(os.close, directory)
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
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
