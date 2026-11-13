"""Write squelch-gated receiver PCM to local MP3 clips."""

import ctypes as c
import errno
import os
import re
import stat
import sys
from array import array
from contextlib import ExitStack, suppress
from ctypes.util import find_library
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO
from unicodedata import category, normalize
from uuid import uuid4

from dab import BAND_III, normalize_service_id
from recording_tones import ToneOnlyClip


SAMPLE_RATE = 24_000
MIN_CLIP_PCM_BYTES = 2 * SAMPLE_RATE * 2  # Two seconds of 16-bit mono PCM.
RECORDINGS_FOLDER = "Recorded Audio"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
CLIP_NAME = re.compile(r"[0-9]{6}-[0-9]{8}(?:-[0-9]+)?\.mp3")
DAB_CLIP_NAME = re.compile(r"[0-9]{6}-[0-9]{6}(?:-[0-9]+)?\.mp3")
FREQUENCY_NAME = re.compile(r"([0-9]{1,4})\.([0-9]{4,6})mhz")
STATION_NAME_BYTES = 120
STATION_FORBIDDEN = frozenset('/\\<>:"|?*')
WINDOWS_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.IGNORECASE)
DAB_FREQUENCIES = frozenset(hz for _, hz in BAND_III)
ID3_HEADER = b"ID3\x03\x00\x00"
CLIP_OWNER = b"sdr-scanner\x00v1:"


class RecordingError(RuntimeError):
    """A clip or its recording folder could not be used."""


@lru_cache(maxsize=1)
def _lame():
    try:
        library = c.CDLL(find_library("mp3lame") or "libmp3lame.so.0")
        signatures = {
            "lame_init": (c.c_void_p, []),
            "lame_init_params": (c.c_int, [c.c_void_p]),
            "lame_encode_buffer": (c.c_int, [c.c_void_p, c.POINTER(c.c_short),
                                             c.POINTER(c.c_short), c.c_int, c.c_void_p, c.c_int]),
            "lame_encode_flush": (c.c_int, [c.c_void_p, c.c_void_p, c.c_int]),
            "lame_close": (c.c_int, [c.c_void_p]),
        }
        for setting in ("in_samplerate", "out_samplerate", "num_channels", "mode",
                        "brate", "quality", "bWriteVbrTag"):
            signatures["lame_set_" + setting] = (c.c_int, [c.c_void_p, c.c_int])
        for name, (result, arguments) in signatures.items():
            function = getattr(library, name)
            function.restype = result
            function.argtypes = arguments
    except (OSError, AttributeError) as exc:
        raise RecordingError("MP3 recording requires libmp3lame0.") from exc
    return library


class _Encoder:
    def __init__(self):
        self.library = _lame()
        self.handle = self.library.lame_init()
        if not self.handle:
            raise RecordingError("Could not open the MP3 encoder.")
        try:
            settings = {"in_samplerate": SAMPLE_RATE, "out_samplerate": SAMPLE_RATE,
                        "num_channels": 1, "mode": 3, "brate": 64, "quality": 3,
                        "bWriteVbrTag": 0}
            for name, value in settings.items():
                if getattr(self.library, "lame_set_" + name)(self.handle, value) < 0:
                    raise RecordingError("Could not configure the MP3 encoder.")
            if self.library.lame_init_params(self.handle) < 0:
                raise RecordingError("Could not start the MP3 encoder.")
        except BaseException:
            self.close()
            raise

    def encode(self, pcm):
        samples = array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        count = len(samples)
        audio = (c.c_short * count).from_buffer(samples)
        output = c.create_string_buffer(7200 + (5 * count + 3) // 4)
        size = self.library.lame_encode_buffer(self.handle, audio, audio, count, output, len(output))
        if size < 0:
            raise RecordingError("Could not encode scanner audio.")
        return c.string_at(output, size)

    def finish(self):
        output = c.create_string_buffer(7200)
        size = self.library.lame_encode_flush(self.handle, output, len(output))
        if size < 0:
            raise RecordingError("Could not finalize the MP3 clip.")
        return c.string_at(output, size)

    def close(self):
        if self.handle:
            self.library.lame_close(self.handle)
            self.handle = None


def _recording_parent(parent):
    try:
        path = Path(parent).expanduser().absolute()
        if ".." not in path.parts and "\0" not in str(path):
            return path
    except (TypeError, ValueError, OSError, RuntimeError) as exc:
        raise RecordingError("Invalid recording folder.") from exc
    raise RecordingError("Invalid recording folder.")


def _recordings_root(parent):
    return parent if parent.name == RECORDINGS_FOLDER else parent / RECORDINGS_FOLDER


def _frequency_folder(hz):
    if type(hz) is not int or hz <= 0:
        raise RecordingError("Choose a tuned frequency first.")
    whole, fractional = divmod(hz, 1_000_000)
    fraction = f"{fractional:06d}".rstrip("0").ljust(4, "0")
    return f"{whole}.{fraction}mhz"


def _station_folder(name):
    if not isinstance(name, str):
        return ""
    name = "".join(" " if character in STATION_FORBIDDEN or category(character).startswith("C")
                   else character for character in normalize("NFC", name))
    name = " ".join(name.split()).strip(" .")
    if (WINDOWS_DEVICE.match(name) or FREQUENCY_NAME.fullmatch(name.lower())
            or name.casefold() == RECORDINGS_FOLDER.casefold()):
        name = "_" + name
    return name.encode("utf-8")[:STATION_NAME_BYTES].decode("utf-8", errors="ignore").rstrip(" .")


def _recording_folder(hz, station_name):
    frequency = _frequency_folder(hz)
    station = _station_folder(station_name)
    return (station, True) if station else (frequency, False)


def _recording_identity(hz, dab_sid):
    if dab_sid is None:
        return b"analogue"
    sid = normalize_service_id(dab_sid)
    if sid is None or type(hz) is not int or hz not in DAB_FREQUENCIES:
        raise RecordingError("Invalid DAB recording identity.")
    return f"dab:{hz}:{sid}".encode("ascii")


def _recording_tag(identity):
    owner_data = CLIP_OWNER + identity
    frame = b"PRIV" + len(owner_data).to_bytes(4, "big") + b"\0\0" + owner_data
    size = bytes((len(frame) >> shift) & 0x7F for shift in (21, 14, 7, 0))
    return ID3_HEADER + size + frame


def _has_identity(directory, name, identity, *, legacy=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(name, flags, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return False
        expected = _recording_tag(identity)
        header = os.read(fd, len(expected))
        if header == expected:
            return True
        return legacy and not header.startswith(b"ID3")
    finally:
        os.close(fd)


def _open_directory(path, *, create=False):
    # Walk with directory descriptors so symlinks and concurrent path replacement
    # cannot redirect the next operation outside the selected directory tree.
    fd = os.open(path.anchor, DIRECTORY_FLAGS)
    try:
        for name in path.parts[1:]:
            try:
                child = os.open(name, DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(name, 0o700, dir_fd=fd)
                child = os.open(name, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _remove_partial(fd, name):
    with suppress(OSError):
        os.unlink(name, dir_fd=fd)


def _close_stream(stream):
    with suppress(OSError):
        stream.close()


@lru_cache(maxsize=1)
def _rename_noreplace():
    function = getattr(c.CDLL(None, use_errno=True), "renameat2", None)
    if function is not None:
        function.argtypes = [c.c_int, c.c_char_p, c.c_int, c.c_char_p, c.c_uint]
        function.restype = c.c_int
    return function


def _publish(fd, partial, filename):
    rename = _rename_noreplace()
    if rename is not None:
        if rename(fd, os.fsencode(partial), fd, os.fsencode(filename), 1) == 0:
            return
        error = c.get_errno()
        if error not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise OSError(error, os.strerror(error), filename)
    os.link(partial, filename, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)


@dataclass
class _PendingClip:
    hz: int
    path: Path
    skip_tone_only: bool
    identity: bytes
    pcm: bytearray = field(default_factory=bytearray)


@dataclass
class _Clip:
    hz: int
    path: Path
    directory: int
    partial: str
    stream: BinaryIO
    encoder: _Encoder
    cleanup: ExitStack
    tone_analysis: ToneOnlyClip | None
    identity: bytes


class Recorder:
    """Record one frequency at a time from a single serialized caller."""

    def __init__(self, parent):
        self._parent = _recording_parent(parent)
        self._pending = None
        self._clip = None

    @property
    def parent(self):
        return self._parent

    @property
    def root(self):
        return _recordings_root(self._parent)

    @property
    def active_hz(self):
        clip = self._active_clip
        return clip.hz if clip else None

    @property
    def active_path(self):
        clip = self._active_clip
        return clip.path if clip else None

    @property
    def _active_clip(self):
        return self._clip or self._pending

    @staticmethod
    def check_available():
        _lame()

    def set_parent(self, parent):
        parent = _recording_parent(parent)
        try:
            os.close(_open_directory(parent))
            with suppress(FileNotFoundError):
                os.close(_open_directory(_recordings_root(parent)))
        except OSError as exc:
            raise RecordingError(f"Could not open the recording folder ({exc.strerror}).") from exc
        if parent != self._parent:
            self.finish()
            self._parent = parent

    def _start(self, pending):
        partial = f".scanner-recording-{uuid4().hex}.part"
        with ExitStack() as cleanup:
            encoder = _Encoder()
            cleanup.callback(encoder.close)
            directory = _open_directory(pending.path.parent, create=True)
            cleanup.callback(os.close, directory)
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            cleanup.callback(_remove_partial, directory, partial)
            try:
                stream = os.fdopen(fd, "wb")
            except BaseException:
                os.close(fd)
                raise
            cleanup.callback(_close_stream, stream)
            stream.write(_recording_tag(pending.identity))
            analysis = ToneOnlyClip(SAMPLE_RATE) if pending.skip_tone_only else None
            return _Clip(pending.hz, pending.path, directory, partial, stream,
                         encoder, cleanup.pop_all(), analysis, pending.identity)

    def feed(self, hz, pcm, *, skip_tone_only=False, station_name=None, dab_sid=None):
        if not pcm:
            return
        if len(pcm) % 2:
            raise RecordingError("Received an incomplete audio sample.")
        name, dab = _recording_folder(hz, station_name)
        identity = _recording_identity(hz, dab_sid)
        folder = self.root / name
        active = self._active_clip
        active_tone_filter = None
        if self._clip:
            active_tone_filter = self._clip.tone_analysis is not None
        elif self._pending:
            active_tone_filter = self._pending.skip_tone_only
        if active and (active.hz != hz or active.path.parent != folder or active.identity != identity
                       or active_tone_filter != skip_tone_only):
            self.finish()
        try:
            if self._clip is None:
                if self._pending is None:
                    timestamp = datetime.now().strftime("%H%M%S-%d%m%y" if dab else "%H%M%S-%d%m%Y")
                    self._pending = _PendingClip(hz, folder / f"{timestamp}.mp3", skip_tone_only, identity)
                pending = self._pending
                if len(pending.pcm) + len(pcm) < MIN_CLIP_PCM_BYTES:
                    pending.pcm.extend(pcm)
                    return
                self._pending = None
                self._clip = self._start(pending)
                if pending.pcm:
                    pcm = pending.pcm + pcm
            clip = self._clip
            if clip.tone_analysis:
                clip.tone_analysis.observe(pcm)
            clip.stream.write(clip.encoder.encode(pcm))
        except (OSError, RecordingError) as exc:
            if self._clip:
                clip, self._clip = self._clip, None
                clip.cleanup.close()
            if isinstance(exc, OSError):
                raise RecordingError(f"Could not write the MP3 clip ({exc.strerror}).") from exc
            raise

    def finish(self):
        self._pending = None
        clip, self._clip = self._clip, None
        if clip is None:
            return None
        try:
            with clip.cleanup:
                if clip.tone_analysis and clip.tone_analysis.should_discard():
                    return None
                clip.stream.write(clip.encoder.finish())
                clip.stream.flush()
                if clip.stream.tell() <= len(_recording_tag(clip.identity)):
                    return None
                os.fsync(clip.stream.fileno())
                path = clip.path
                collision = 1
                while True:
                    try:
                        _publish(clip.directory, clip.partial, path.name)
                        return path
                    except FileExistsError:
                        collision += 1
                        path = clip.path.with_stem(f"{clip.path.stem}-{collision}")
        except OSError as exc:
            raise RecordingError(f"Could not save the MP3 clip ({exc.strerror}).") from exc

    def clear_frequency(self, hz, *, station_name=None, dab_sid=None):
        name, dab = _recording_folder(hz, station_name)
        identity = _recording_identity(hz, dab_sid)
        legacy = not dab and dab_sid is None and hz not in DAB_FREQUENCIES
        clip_name = DAB_CLIP_NAME if dab else CLIP_NAME
        active = self._active_clip
        if active and active.hz == hz and active.path.parent.name == name and active.identity == identity:
            self.finish()
        removed = 0
        try:
            with ExitStack() as cleanup:
                try:
                    root = _open_directory(self.root)
                    cleanup.callback(os.close, root)
                    folder = os.open(name, DIRECTORY_FLAGS, dir_fd=root)
                    cleanup.callback(os.close, folder)
                except FileNotFoundError:
                    return 0
                with os.scandir(folder) as entries:
                    for entry in entries:
                        if clip_name.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                            try:
                                if not _has_identity(folder, entry.name, identity, legacy=legacy):
                                    continue
                                os.unlink(entry.name, dir_fd=folder)
                            except OSError as exc:
                                if exc.errno not in (errno.ENOENT, errno.ELOOP):
                                    raise
                                continue
                            removed += 1
                try:
                    os.rmdir(name, dir_fd=root)
                except OSError as exc:
                    if exc.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                        raise
        except OSError as exc:
            raise RecordingError(f"Could not clear recordings for {name} ({exc.strerror}).") from exc
        return removed
