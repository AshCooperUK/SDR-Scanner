"""Search and resolve finalized scanner recordings for read-only playback."""

import hashlib
import os
import re
import stat
from contextlib import ExitStack
from datetime import datetime
from heapq import nlargest
from urllib.parse import quote, unquote

from recording import (
    CLIP_NAME, DAB_CLIP_NAME, DIRECTORY_FLAGS, FLAT_CLIP_NAME, FREQUENCY_NAME, V10_CLIP_NAME,
    V10_LEGACY_CLIP_NAME, _frequency_folder, _open_directory, _station_folder, is_saved_channel_folder,
)
from scanner import MANUAL_MAX_HZ, MANUAL_MIN_HZ


RECORDINGS_URL = "/api/recordings/"
RECORDINGS_SEARCH_URL = RECORDINGS_URL + "search"
RECORDINGS_PAGE_SIZE = 10
# Page numbers travel through JavaScript clients. Limit the request to an exact
# JSON integer, not an arbitrary number of clips in the recording archive.
MAX_RECORDINGS_PAGE = (1 << 53) - 1
SEARCH_FREQUENCY = re.compile(r"([0-9]{0,4})(?:\.([0-9]{3}\.[0-9]{3}|[0-9]{1,6}))?(?:\s*mhz)?",
                              re.IGNORECASE)
SEARCH_WORD = re.compile(r"\b(?:(?P<bank>bank)\s+|ch\s*)(?P<number>[0-9]+)\b|\S+")
CLIP_ID = re.compile(r"[0-9a-f]{24}")
BYTE_RANGE = re.compile(r"bytes=([0-9]{0,20})-([0-9]{0,20})", re.IGNORECASE)


def _frequency_from_name(name):
    match = FREQUENCY_NAME.fullmatch(name)
    if match is not None:
        whole, fraction = match.groups()
        hz = int(whole) * 1_000_000 + int(fraction.ljust(6, "0"))
        if MANUAL_MIN_HZ <= hz <= MANUAL_MAX_HZ and _frequency_folder(hz) == name:
            return hz
    return None


def _clip_pattern(folder):
    if _frequency_from_name(folder) is not None:
        return CLIP_NAME
    if folder and _station_folder(folder) == folder:
        return DAB_CLIP_NAME
    return None


def _frequency_label(name):
    return f"{name.removesuffix('mhz')} MHz" if _frequency_from_name(name) is not None else name


def _v10_clip(name):
    match = V10_CLIP_NAME.fullmatch(name)
    if match is None:
        return None
    frequency = match["frequency"]
    whole, fraction = frequency.split(".")
    hz = int(whole) * 1_000_000 + int(fraction)
    if whole == str(int(whole)) and MANUAL_MIN_HZ <= hz <= MANUAL_MAX_HZ:
        return f"{frequency} MHz", match["stamp"]
    return None


def _flat_clip(name, *, v10_only=False):
    if clip := _v10_clip(name):
        return clip
    match = V10_LEGACY_CLIP_NAME.fullmatch(name)
    if match is not None:
        prefix = match["frequency"] + "mhz"
        return (f"{match['frequency']} MHz", match["stamp"]) if _frequency_from_name(prefix) is not None else None
    if v10_only:
        return None
    match = FLAT_CLIP_NAME.fullmatch(name)
    if match and _clip_pattern(match["prefix"]) is not None:
        return _frequency_label(match["prefix"]), match["stamp"]
    return None


def _nested_clip(folder, name, *, v10_only=False):
    if is_saved_channel_folder(folder) and (clip := _v10_clip(name)):
        frequency, stamp = clip
        return f"{folder} · {frequency}", stamp
    if not v10_only and (pattern := _clip_pattern(folder)) is not None and pattern.fullmatch(name):
        return _frequency_label(folder), name
    return None


def _finalized(info):
    return stat.S_ISREG(info.st_mode) and info.st_size > 0 and info.st_nlink == 1


def _fingerprint(info):
    identity = f"{info.st_dev}:{info.st_ino}:{info.st_ctime_ns}:{info.st_mtime_ns}:{info.st_size}"
    return hashlib.sha256(identity.encode("ascii")).hexdigest()[:24]


def _recording_roots(root, older_roots):
    roots = tuple(dict.fromkeys((root, *older_roots)))
    for index, candidate in enumerate(roots):
        # Keep old layouts inside their dedicated folder when the exact V10
        # destination is a separate directory selected by the user.
        yield candidate, index == 0 and len(roots) > 1


def _list_clips(directory, *, v10_only):
    with os.scandir(directory) as entries:
        for entry in entries:
            if layout := _flat_clip(entry.name, v10_only=v10_only):
                yield "", entry, layout
            if not entry.is_dir(follow_symlinks=False):
                continue
            if not is_saved_channel_folder(entry.name) and (v10_only or _clip_pattern(entry.name) is None):
                continue
            try:
                folder = os.open(entry.name, DIRECTORY_FLAGS, dir_fd=directory)
            except OSError:
                continue
            try:
                with os.scandir(folder) as clips:
                    for clip in clips:
                        if layout := _nested_clip(entry.name, clip.name, v10_only=v10_only):
                            yield entry.name, clip, layout
            finally:
                os.close(folder)


def _finalized_clips(root, older_roots):
    for candidate_root, v10_only in _recording_roots(root, older_roots):
        try:
            directory = _open_directory(candidate_root)
        except FileNotFoundError:
            continue
        except OSError:
            if candidate_root == root:
                raise
            continue
        try:
            for folder, clip, layout in _list_clips(directory, v10_only=v10_only):
                try:
                    info = clip.stat(follow_symlinks=False)
                except OSError:
                    continue
                if _finalized(info):
                    yield folder, clip.name, layout, info
        finally:
            os.close(directory)


def _clip_order(clip):
    folder, name, _, info = clip
    return info.st_mtime_ns, info.st_ctime_ns, folder, name


def _playback_link(folder, name, layout, info):
    label, stamp = layout
    encoded_name = quote(name, safe="")
    route = f"{quote(folder, safe='')}/{encoded_name}" if folder else encoded_name
    fingerprint = _fingerprint(info)
    return {"url": f"{RECORDINGS_URL}{route}/{fingerprint}", "label": f"{label} · {stamp}"}


def latest_clip(root, *older_roots):
    """Find the most recently finalized clip without following directory links."""
    clip = max(_finalized_clips(root, older_roots), key=_clip_order, default=None)
    return _playback_link(*clip) if clip else None


def recording_filters(action):
    if not isinstance(action, dict) or action.keys() - {"query", "date", "mhz", "page"}:
        raise ValueError("Invalid recording search.")
    query, date_text, mhz = (action.get(key, "") for key in ("query", "date", "mhz"))
    if any(not isinstance(value, str) or len(value) > limit
           for value, limit in ((query, 120), (date_text, 16), (mhz, 20))):
        raise ValueError("Invalid recording search.")
    page = action.get("page", 1)
    if type(page) is not int or not 1 <= page <= MAX_RECORDINGS_PAGE:
        raise ValueError("Invalid recordings page.")
    day = None
    if date_text := date_text.strip():
        try:
            day = datetime.strptime(date_text, "%d.%m.%Y" if "." in date_text else "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("Enter a date as DD.MM.YYYY or YYYY-MM-DD.") from None
    hz = None
    if mhz := mhz.strip():
        match = SEARCH_FREQUENCY.fullmatch(mhz)
        if match is None or not any(match.groups()):
            raise ValueError("Enter a frequency in MHz, e.g. 135.375.")
        whole, fraction = match.groups()
        hz = int(whole or "0") * 1_000_000 + int((fraction or "").replace(".", "").ljust(6, "0"))
        if not MANUAL_MIN_HZ <= hz <= MANUAL_MAX_HZ:
            raise ValueError("The frequency is outside the scanner range.")
    terms = []
    for match in SEARCH_WORD.finditer(query.casefold()):
        if match["number"] is not None:
            prefix = "bank " if match["bank"] else "ch"
            terms.append(rf"\b{prefix}0*{int(match['number'])}\b")
        else:
            terms.append(re.escape(match[0]))
    return tuple(re.compile(term) for term in terms), day, hz, page


def _clip_details(folder, name):
    if match := V10_CLIP_NAME.fullmatch(name) or V10_LEGACY_CLIP_NAME.fullmatch(name):
        whole, fraction = match["frequency"].split(".")
        hz = int(whole) * 1_000_000 + int(fraction.ljust(6, "0"))
        stamp = match["stamp"]
        pattern = "%H:%M:%S - %d.%m.%Y" if "." in stamp else "%H:%M:%S - %d%m%Y"
    else:
        match = FLAT_CLIP_NAME.fullmatch(name) if not folder else None
        hz = _frequency_from_name(match["prefix"] if match else folder)
        stamp = match["stamp"] if match else name
        time, day, *_ = stamp.removesuffix(".mp3").split("-")
        stamp = f"{time}-{day}"
        pattern = "%H%M%S-%d%m%Y" if len(day) == 8 else "%H%M%S-%d%m%y"
    try:
        return hz, datetime.strptime(stamp, pattern)
    except ValueError:
        # Older layouts allow date strings that may be malformed.
        return hz, None


def search_recordings(root, *older_roots, filters):
    """Return a page of playable clips with each result scoped to the configured roots."""
    terms, day, hz, page = filters
    total = 0

    def matches():
        nonlocal total
        for clip in _finalized_clips(root, older_roots):
            folder, name, (label, _), _ = clip
            if terms:
                text = f"{folder} {name} {label}".casefold()
                if not all(term.search(text) for term in terms):
                    continue
            frequency, stamp = _clip_details(folder, name)
            if day is not None and (stamp is None or stamp.date() != day):
                continue
            if hz is not None and frequency != hz:
                continue
            total += 1
            yield clip, frequency, stamp

    finishing = nlargest(page * RECORDINGS_PAGE_SIZE, matches(), key=lambda item: (
        item[2] or datetime.min, _clip_order(item[0]),
    ))
    pages = max(1, (total + RECORDINGS_PAGE_SIZE - 1) // RECORDINGS_PAGE_SIZE)
    page = min(page, pages)
    start = (page - 1) * RECORDINGS_PAGE_SIZE
    clips = []
    for clip, frequency, stamp in finishing[start:start + RECORDINGS_PAGE_SIZE]:
        folder, name, _, _ = clip
        clips.append({**_playback_link(*clip), "folder": folder, "name": name, "hz": frequency,
                      "date": stamp.date().isoformat() if stamp else None})
    return {"clips": clips, "page": page, "pages": pages, "total": total}


def open_clip(root, route, *older_roots):
    """Open the exact file selected for replay. The caller owns the stream."""
    parts = route.split("/")
    if len(parts) not in (2, 3) or CLIP_ID.fullmatch(parts[-1]) is None:
        raise FileNotFoundError
    fingerprint = parts[-1]
    try:
        folder_name = unquote(parts[0], errors="strict") if len(parts) == 3 else None
        name = unquote(parts[-2], errors="strict")
    except UnicodeError:
        raise FileNotFoundError from None
    last_error = FileNotFoundError()
    for candidate_root, v10_only in _recording_roots(root, older_roots):
        clip = (_flat_clip(name, v10_only=v10_only) if folder_name is None
                else _nested_clip(folder_name, name, v10_only=v10_only))
        if clip is None:
            continue
        try:
            return _open_selected_clip(candidate_root, folder_name, name, fingerprint)
        except OSError as error:
            last_error = error
    raise last_error


def _open_selected_clip(root, folder_name, name, fingerprint):
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
