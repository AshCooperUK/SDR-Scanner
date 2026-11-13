"""Store the remote access policy and manage browser unlocks."""

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import threading
import time
from collections import deque
from contextlib import contextmanager
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit


PIN_PATTERN = re.compile(r"[0-9]{6}")
SESSION_PATTERN = re.compile(r"[0-9a-f]{32}")
CAPABILITY_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
REMOTE_URL_LIMIT = 270
HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
REMOTE_AUTHORITY = re.compile(rf"(\[[0-9a-f:.]+\]|{HOST_LABEL}(?:\.{HOST_LABEL})*\.?)(?::([1-9][0-9]{{0,4}}))?")
PIN_FILE_LIMIT = 1024
PIN_ITERATIONS = 600_000
SALT_BYTES = 32
ACCESS_SECONDS = 8 * 60 * 60
MAX_GRANTS = 256
ATTEMPT_WINDOW = 300
SOURCE_ATTEMPTS = 5
TOTAL_ATTEMPTS = 20


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _digest(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode("ascii"), salt, PIN_ITERATIONS)


def normalize_remote_url(value):
    """Return a canonical HTTP(S) origin, or an empty string for LAN only."""
    error = "Enter one remote URL such as https://scanner.example.com, or leave it blank for LAN only."
    if not isinstance(value, str) or len(value) > REMOTE_URL_LIMIT:
        raise ValueError(error)
    value = value.strip(" ")
    if not value:
        return ""
    if any(not 33 <= ord(char) <= 126 or char in "?#" for char in value):
        raise ValueError(error)
    try:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or parts.path not in ("", "/"):
            raise ValueError
        match = REMOTE_AUTHORITY.fullmatch(parts.netloc.lower())
        if match is None:
            raise ValueError
        host, port = match.groups()
        if len(host) > 254 or port and int(port) > 65535:
            raise ValueError
        if host.startswith("["):
            host = f"[{IPv6Address(host[1:-1])}]"
        elif re.fullmatch(r"[0-9.]+", host):
            host = str(IPv4Address(host))
    except ValueError:
        raise ValueError(error) from None
    default_port = "443" if parts.scheme == "https" else "80"
    suffix = f":{port}" if port and port != default_port else ""
    return f"{parts.scheme}://{host}{suffix}"


class AccessConflict(ValueError):
    """An access policy changed since it was downloaded."""


class AccessPin:
    def __init__(self, path):
        self.path = Path(path)
        self._credential, self._remote_url = self._load()
        self._revision = secrets.token_hex(16)
        self._lock = threading.RLock()
        self._attempts = deque()
        self._grants = {}

    @property
    def configured(self):
        with self._lock:
            return self._credential is not None

    @property
    def remote_url(self):
        with self._lock:
            return self._remote_url

    def snapshot(self):
        with self._lock:
            return {"remote_url": self._remote_url, "pin_configured": self._credential is not None,
                    "revision": self._revision}

    def guard(self):
        """Hold policy and PIN state constant while checking one request."""
        return self._lock

    def _load(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None, None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError("The access PIN file must be owned by the service user with mode 0600.")
            payload = stream.read(PIN_FILE_LIMIT + 1)
        if len(payload) > PIN_FILE_LIMIT:
            raise ValueError("The access PIN file is too large.")
        try:
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ValueError
            remote_url = normalize_remote_url(data.pop("remote_url")) if "remote_url" in data else None
            credential = None
            if data.keys() == {"salt", "digest"}:
                salt, digest = bytes.fromhex(data["salt"]), bytes.fromhex(data["digest"])
                if len(salt) != SALT_BYTES or len(digest) != hashlib.sha256().digest_size:
                    raise ValueError
                credential = salt, digest
            elif data or remote_url is None:
                raise ValueError
        except (TypeError, ValueError, RecursionError):
            raise ValueError("The scanner access settings file is invalid.") from None
        return credential, remote_url

    @contextmanager
    def _stage(self, credential, remote_url):
        data = {} if remote_url is None else {"remote_url": remote_url}
        if credential is not None:
            salt, digest = credential
            data.update(salt=salt.hex(), digest=digest.hex())
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                    prefix=self.path.name + ".", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            yield temporary
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _make_credential(pin):
        if not _matches(PIN_PATTERN, pin):
            raise ValueError("Enter a six-digit PIN.")
        salt = secrets.token_bytes(SALT_BYTES)
        return salt, _digest(pin, salt)

    def _commit(self, temporary, credential, remote_url):
        temporary.replace(self.path)
        self._credential = credential
        self._remote_url = remote_url
        self._revision = secrets.token_hex(16)
        self._attempts.clear()
        self._grants.clear()

    @contextmanager
    def program(self, changes, revision):
        """Validate and stage CHIRP settings, then yield a one-use commit callback."""
        if not isinstance(changes, dict) or changes.keys() - {"new_pin", "remote_url"}:
            raise ValueError("Invalid scanner access settings.")
        remote_url = normalize_remote_url(changes["remote_url"]) if "remote_url" in changes else None
        credential = self._make_credential(changes["new_pin"]) if "new_pin" in changes else None
        with self._lock:
            if revision != self._revision:
                raise AccessConflict("Remote access settings changed. Download From Radio again before uploading.")
            if remote_url is None:
                remote_url = self._remote_url
            if credential is None:
                credential = self._credential
            if remote_url == self._remote_url and credential is self._credential:
                yield lambda: None
                return
            with self._stage(credential, remote_url) as temporary:
                yield lambda: self._commit(temporary, credential, remote_url)

    def set_pin(self, pin):
        credential = self._make_credential(pin)
        with self._lock, self._stage(credential, self._remote_url) as temporary:
            self._commit(temporary, credential, self._remote_url)

    def _retry_after(self, source, now):
        while self._attempts and now - self._attempts[0][0] >= ATTEMPT_WINDOW:
            self._attempts.popleft()
        source_attempts = [timestamp for timestamp, address in self._attempts if address == source]
        retry_at = now
        if len(source_attempts) >= SOURCE_ATTEMPTS:
            retry_at = source_attempts[0] + ATTEMPT_WINDOW
        if len(self._attempts) >= TOTAL_ATTEMPTS:
            retry_at = max(retry_at, self._attempts[0][0] + ATTEMPT_WINDOW)
        return math.ceil(retry_at - now)

    def unlock(self, pin, source, session, *, revision=None):
        if not _matches(PIN_PATTERN, pin) or not _matches(SESSION_PATTERN, session):
            return None, 0
        with self._lock:
            credential = self._credential
            if credential is None or revision is not None and revision != self._revision:
                return None, 0
            revision = self._revision
            now = time.monotonic()
            if retry_after := self._retry_after(source, now):
                return None, retry_after
            attempt = (now, source)
            self._attempts.append(attempt)
        salt, digest = credential
        if not hmac.compare_digest(_digest(pin, salt), digest):
            return None, 0
        with self._lock:
            if revision != self._revision:
                return None, 0
            if attempt in self._attempts:
                self._attempts.remove(attempt)
            return self._grant(session, time.monotonic()), 0

    def _grant(self, session, now):
        for expired in [key for key, (_, expiry) in self._grants.items() if expiry <= now]:
            del self._grants[expired]
        self._grants.pop(session, None)
        if len(self._grants) >= MAX_GRANTS:
            del self._grants[next(iter(self._grants))]
        capability = secrets.token_urlsafe(32)
        self._grants[session] = capability, now + ACCESS_SECONDS
        return capability

    def authorized(self, capability, session):
        if not _matches(CAPABILITY_PATTERN, capability) or not _matches(SESSION_PATTERN, session):
            return False
        with self._lock:
            grant = self._grants.get(session)
            if grant is None:
                return False
            expected, expiry = grant
            if expiry <= time.monotonic():
                del self._grants[session]
                return False
            return hmac.compare_digest(capability, expected)
