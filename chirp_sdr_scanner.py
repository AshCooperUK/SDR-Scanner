"""CHIRP clone-mode driver for the SDr Scanner web application."""

from copy import deepcopy
from ipaddress import IPv4Address, IPv6Address
import json
import re
import sys
from unicodedata import category
from urllib.parse import urlsplit
from weakref import WeakSet, ref

from chirp import chirp_common, directory, errors, memmap, settings


_PORT = re.compile(r"socket://127\.0\.0\.1:([1-9][0-9]{0,4})")
_REVISION = re.compile(r"[0-9a-f]{64}")
_ACCESS_REVISION = re.compile(r"[0-9a-f]{32}")
_PIN = re.compile(r"[0-9]{6}")
_REMOTE_URL_LIMIT = 270
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_REMOTE_AUTHORITY = re.compile(
    rf"(\[[0-9a-f:.]+\]|{_HOST_LABEL}(?:\.{_HOST_LABEL})*\.?)(?::([1-9][0-9]{{0,4}}))?")
_NEW_PIN_SETTING = "sdr_scanner_new_pin"
_MAX_PAYLOAD = 8 * 1024 * 1024
_MAX_ZONE_CHANNELS = 8192
_FAVOURITE_COUNT = 124
_MIN_HZ = 100_000
_MAX_HZ = 1_750_000_000
_NAME_LIMIT = 80
_INVALID_NAME_CATEGORIES = {"Cc", "Cs", "Zl", "Zp"}
_IMAGE_PREFIX = b"SDrScanner CHIRP 1\n"
_MODES = {"FM": "NFM", "AM": "AM", "WFM": "WFM", "USB": "USB", "LSB": "LSB"}
_SCANNER_MODES = {value: key for key, value in _MODES.items()} | {"FM": "FM"}
_ZONES = {
    "PMR": "PMR 446", "AIR": "Air", "MRN": "Marine", "CBAM": "CB AM",
    "CBFM": "CB FM", "BUS": "Business", "HAM": "Amateur", "BCAST": "FM Radio",
    "RAIL": "Rail", "EVT": "Events", "UTIL": "Utilities",
}
_CHOICES = {
    "plan": ("Band plan", {1: "UK / International", 2: "German"}),
    "general_step": ("General step", {0: "Auto", 5000: "5 kHz", 6250: "6.25 kHz",
                                     10000: "10 kHz", 12500: "12.5 kHz"}),
    "air_step": ("Airband step", {8333: "8.33 kHz", 12500: "12.5 kHz"}),
}
_SWITCHES = {"rds_enabled": "RDS", "program_locked": "Program lock",
             "close_alert": "Close Call alert"}
_CLOSE_GROUPS = ("Low band", "Airband", "High band", "UHF", "800 MHz")
_SETTINGS = {*_CHOICES, *_SWITCHES, "clock_offset", "close_groups"}
_ZONE_READ_ONLY_COLUMNS = ("comment", "duplex", "offset")


def _encode(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _setting_values(values):
    if not isinstance(values, dict) or set(values) != _SETTINGS:
        raise ValueError("Invalid scanner settings")
    for key, (_, choices) in _CHOICES.items():
        if type(values[key]) is not int or values[key] not in choices:
            raise ValueError("Invalid band plan or step")
    if any(type(values[key]) is not bool for key in _SWITCHES):
        raise ValueError("Invalid scanner switch setting")
    if type(values["clock_offset"]) is not int or not -1439 <= values["clock_offset"] <= 1439:
        raise ValueError("Invalid clock offset")
    groups = values["close_groups"]
    if (not isinstance(groups, list) or len(groups) != len(_CLOSE_GROUPS)
            or any(type(value) is not bool for value in groups) or not any(groups)):
        raise ValueError("Enable at least one Close Call band")
    return values


def _validate_remote_url(value):
    try:
        if not isinstance(value, str) or len(value) > _REMOTE_URL_LIMIT:
            raise ValueError
        value = value.strip(" ")
        if not value:
            return ""
        if any(not 33 <= ord(char) <= 126 or char in "?#" for char in value):
            raise ValueError
        url = urlsplit(value)
        authority = _REMOTE_AUTHORITY.fullmatch(url.netloc.lower())
        if url.scheme not in ("http", "https") or url.path not in ("", "/") or authority is None:
            raise ValueError
        host, port = authority.groups()
        if len(host) > 254 or port and int(port) > 65535:
            raise ValueError
        if host.startswith("["):
            host = f"[{IPv6Address(host[1:-1])}]"
        elif re.fullmatch(r"[0-9.]+", host):
            host = str(IPv4Address(host))
    except ValueError:
        raise ValueError("Enter an HTTP or HTTPS origin such as https://scanner.example, or leave it blank") from None
    default_port = "443" if url.scheme == "https" else "80"
    suffix = f":{port}" if port and port != default_port else ""
    return f"{url.scheme}://{host}{suffix}"


def _validate_access(access):
    if (not isinstance(access, dict) or access.keys() != {"remote_url", "pin_configured", "revision"}
            or type(access["pin_configured"]) is not bool
            or not isinstance(access["revision"], str) or not _ACCESS_REVISION.fullmatch(access["revision"])
            or access["remote_url"] is not None and not isinstance(access["remote_url"], str)):
        raise ValueError("Invalid scanner remote access settings")
    if access["remote_url"] is not None:
        _validate_remote_url(access["remote_url"])


class _NewPinValue(settings.RadioSettingValueString):
    def __init__(self, pending_pin, radio):
        super().__init__(0, 6, pending_pin, autopad=False)
        self.radio = ref(radio)

    def set_value(self, value):
        if not isinstance(value, str) or value and not _PIN.fullmatch(value):
            raise settings.InvalidValueError("Enter a six-digit PIN or leave the field blank")
        super().set_value(value)

    def __str__(self):
        return "******" if self.get_value() else ""


def _valid_frequency(hz):
    return type(hz) is int and _MIN_HZ <= hz <= _MAX_HZ


def _name_is_valid(name):
    return (isinstance(name, str) and len(name) <= _NAME_LIMIT
            and all(category(char) not in _INVALID_NAME_CATEGORIES for char in name))


def _valid_tone(tone):
    if not isinstance(tone, dict):
        return False
    if tone.get("kind") == "CTCSS" and tone.keys() == {"kind", "hz"}:
        return type(tone["hz"]) in (int, float) and tone["hz"] in chirp_common.TONES
    if tone.get("kind") == "DCS" and tone.keys() == {"kind", "code", "polarity"}:
        code = tone["code"]
        return (isinstance(code, str) and re.fullmatch(r"[0-7]{3}", code)
                and int(code) in chirp_common.DTCS_CODES and tone["polarity"] in ("N", "I"))
    return False


def _validate_entry(entry, *, favourite):
    fields = {"hz", "mode", "name", "tone"} if favourite else {"hz", "mode", "number"}
    if (not isinstance(entry, dict) or entry.keys() != fields or not _valid_frequency(entry["hz"])
            or not isinstance(entry["mode"], str) or entry["mode"] not in _MODES):
        raise ValueError("Invalid channel frequency or mode")
    if favourite:
        tone = entry["tone"]
        if not _name_is_valid(entry["name"]) or (tone is not None and (
                entry["mode"] != "FM" or not _valid_tone(tone))):
            raise ValueError("Invalid favourite name or receive tone")
    else:
        number = entry["number"]
        if number is not None and (type(number) is not int or not 1 <= number <= 9999):
            raise ValueError("Invalid zone display channel number")


def _validate_image(image, *, working=False):
    if (not isinstance(image, dict) or type(image.get("version")) is not int or image["version"] != 1
            or not isinstance(image.get("revision"), str) or not _REVISION.fullmatch(image["revision"])):
        raise ValueError("Unsupported scanner image or protocol version")
    channels = image.get("channels")
    reserved = image.get("reserved")
    zones = image.get("zones")
    if (not isinstance(channels, list) or len(channels) != _FAVOURITE_COUNT
            or not isinstance(reserved, list)
            or any(type(slot) is not int or not 1 <= slot <= _FAVOURITE_COUNT
                   or channels[slot - 1] is not None for slot in reserved)
            or len(set(reserved)) != len(reserved)
            or not isinstance(zones, dict) or set(zones) != set(_ZONES)
            or any(not isinstance(entries, list) or len(entries) > _MAX_ZONE_CHANNELS
                   for entries in zones.values())):
        raise ValueError("Invalid scanner channel data")
    for channel in channels:
        if channel is not None:
            _validate_entry(channel, favourite=True)
    for entries in zones.values():
        for entry in entries:
            if entry is not None or not working:
                _validate_entry(entry, favourite=False)
    _setting_values(image.get("settings"))
    if "access" in image:
        _validate_access(image["access"])
    return image


def _features(bounds, *, favourites):
    features = chirp_common.RadioFeatures()
    features.memory_bounds = bounds
    features.has_bank = False
    features.has_name = favourites
    # CHIRP inserts expanded zone fields before Comment. The GTK grid needs
    # that column visible so its labels exist when CHIRP inserts the field.
    features.has_comment = not favourites
    features.valid_name_length = _NAME_LIMIT if favourites else 0
    features.valid_characters = ""
    features.valid_bands = [(_MIN_HZ, _MAX_HZ + 1)]
    features.valid_modes = list(_SCANNER_MODES)
    # Accept source-radio split frequencies without CHIRP first converting
    # their transmit frequencies to offsets. Only RX is stored by this driver.
    features.valid_duplexes = ["off", "split"]
    features.has_offset = False
    features.has_tuning_step = False
    features.has_nostep_tuning = True
    features.valid_skips = []
    features.has_dtcs = favourites
    features.has_rx_dtcs = favourites
    features.has_dtcs_polarity = favourites
    features.has_ctone = favourites
    features.has_cross = favourites
    features.valid_tmodes = ["", "Cross", "TSQL", "DTCS"] if favourites else [""]
    features.valid_cross_modes = ["->Tone", "->DTCS"] if favourites else []
    return features


def _receive_tone(memory):
    if memory.tmode not in ("", "Tone", "Cross", "TSQL", "DTCS"):
        raise ValueError("Choose a receive tone or no tone")
    _, (kind, value, polarity) = chirp_common.split_tone_encode(memory)
    if kind == "Tone" and value in chirp_common.TONES:
        return {"kind": "CTCSS", "hz": value}
    if kind == "DTCS" and value in chirp_common.DTCS_CODES:
        return {"kind": "DCS", "code": f"{value:03}", "polarity": "I" if polarity == "R" else "N"}
    if kind:
        raise ValueError("Unsupported receive tone")


def _filter_name(name):
    return "".join(char for char in name if category(char) not in _INVALID_NAME_CATEGORIES).strip()[:_NAME_LIMIT]


def _encode_memory(memory, favourites):
    if memory.empty:
        return None
    if not _valid_frequency(memory.freq):
        raise ValueError("Enter a frequency between 100 kHz and 1.75 GHz")
    if memory.mode not in _SCANNER_MODES:
        raise ValueError("Choose AM, NFM, WFM, USB or LSB")
    mode = _SCANNER_MODES[memory.mode]
    entry = {"hz": memory.freq, "mode": mode}
    if favourites:
        name = memory.name.strip()
        if not _name_is_valid(name):
            raise ValueError("Enter a single-line name of at most 80 characters")
        tone = _receive_tone(memory)
        if tone and mode != "FM":
            raise ValueError("Receive tones require narrow FM")
        entry.update(name=name, tone=tone)
    return entry


class _ZoneRadio(chirp_common.Radio):
    """A CHIRP editor view over one section of the parent's clone image."""

    def __init__(self, parent, zone):
        super().__init__(None)
        self._parent = parent
        self._zone = zone
        self._sdr_scanner_zone = zone is not None
        self.VENDOR = parent.VENDOR
        self.MODEL = parent.MODEL
        self.VARIANT = _ZONES.get(zone, "Favourites")
        self._size = _FAVOURITE_COUNT if zone is None else min(
            _MAX_ZONE_CHANNELS, len(self._entries()) + 32)

    def _entries(self):
        if self._zone is None:
            return self._parent._working["channels"]
        return self._parent._working["zones"][self._zone]

    def _reserved(self, number):
        return self._zone is None and number in self._parent._base["reserved"]

    def get_features(self):
        return _features((1, self._size), favourites=self._zone is None)

    def filter_name(self, name):
        return _filter_name(name) if self._zone is None else ""

    def _index(self, number):
        if type(number) is not int or not 1 <= number <= self._size:
            raise errors.InvalidMemoryLocation("Channel number is outside this zone")
        return number - 1

    def get_memory(self, number):
        index = self._index(number)
        entries = self._entries()
        entry = entries[index] if index < len(entries) else None
        memory = chirp_common.Memory(number, empty=entry is None)
        memory.mode = "NFM"
        memory.duplex = "off"
        memory.offset = 0
        if entry:
            memory.freq = entry["hz"]
            memory.mode = _MODES[entry["mode"]]
            if self._zone is None:
                memory.name = entry["name"]
                tone = entry["tone"]
                if tone and tone["kind"] == "CTCSS":
                    receive = "Tone", tone["hz"], None
                    chirp_common.split_tone_decode(memory, ("", None, None), receive)
                elif tone and tone["kind"] == "DCS":
                    receive = "DTCS", int(tone["code"]), "R" if tone["polarity"] == "I" else "N"
                    chirp_common.split_tone_decode(memory, ("", None, None), receive)
        if self._zone is not None:
            number_value = (entry["number"] or 0) if entry else 0
            memory.extra = settings.RadioSettingGroup(
                "zone_channel", "Zone channel",
                settings.RadioSetting("display_number", "Display channel number (0 for none)",
                                      settings.RadioSettingValueInteger(0, 9999, number_value)),
            )
            memory.immutable = list(_ZONE_READ_ONLY_COLUMNS)
        if self._reserved(number):
            memory.immutable = ["empty", "freq", "mode", "name", "tmode", "cross_mode",
                                "rtone", "ctone", "dtcs", "rx_dtcs", "dtcs_polarity"]
        if self._zone is None:
            memory.immutable += ["duplex", "offset"]
        return memory

    def validate_memory(self, memory):
        try:
            _encode_memory(memory, self._zone is None)
        except (TypeError, ValueError) as error:
            return [chirp_common.ValidationError(str(error))]
        return []

    def check_set_memory_immutable_policy(self, existing, new):
        # These fields are read-only in the grid but discarded on import.
        # Apply CHIRP's normal checks to the remaining fields, including DAB.
        if self._reserved(existing.number):
            raise errors.InvalidMemoryLocation("DAB favourites can only be edited on the scanner")
        existing = existing.dupe()
        existing.immutable = [field for field in existing.immutable
                              if field not in _ZONE_READ_ONLY_COLUMNS]
        super().check_set_memory_immutable_policy(existing, new)

    def set_memory(self, memory):
        index = self._index(memory.number)
        if self._reserved(memory.number):
            raise errors.InvalidMemoryLocation("DAB favourites can only be edited on the scanner")
        try:
            entry = _encode_memory(memory, self._zone is None)
        except (TypeError, ValueError) as error:
            raise errors.InvalidDataError(str(error)) from error
        entries = self._entries()
        if self._zone is not None and entry is not None:
            previous = entries[index] if index < len(entries) else None
            entry["number"] = previous["number"] if previous else None
            for setting in memory.extra:
                if setting.get_name() == "display_number":
                    entry["number"] = int(setting.value) or None
        if index >= len(entries):
            if entry is None:
                return
            entries.extend([None] * (index + 1 - len(entries)))
        entries[index] = entry

    def erase_memory(self, number):
        self.set_memory(chirp_common.Memory(number, empty=True))


@directory.register
class SDrScannerRadio(chirp_common.CloneModeRadio):
    VENDOR = "SDr Scanner"
    MODEL = "Web Scanner"
    WANTS_RTS = False
    WANTS_DTR = False

    def __init__(self, pipe):
        self._base = None
        self._working = None
        self._pending_pin = ""
        self._access_editors = WeakSet()
        super().__init__(pipe)

    def get_features(self):
        features = _features((1, _FAVOURITE_COUNT), favourites=True)
        features.has_sub_devices = True
        features.has_settings = True
        return features

    @classmethod
    def get_prompts(cls):
        prompts = chirp_common.RadioPrompts()
        prompts.pre_download = (
            "Start SDr Scanner, then select the custom port socket://127.0.0.1:8766."
        )
        prompts.pre_upload = (
            "Upload sends the edits in this image. Download again if the scanner "
            "has changed since this image was created."
        )
        return prompts

    def get_sub_devices(self):
        return [_ZoneRadio(self, zone) for zone in (None, *_ZONES)]

    def get_memory(self, number):
        return _ZoneRadio(self, None).get_memory(number)

    def filter_name(self, name):
        return _filter_name(name)

    def set_memory(self, memory):
        _ZoneRadio(self, None).set_memory(memory)

    def check_set_memory_immutable_policy(self, existing, new):
        _ZoneRadio(self, None).check_set_memory_immutable_policy(existing, new)

    def get_settings(self):
        values = self._working["settings"]
        receiver = settings.RadioSettingGroup("receiver", "Receiver")
        close = settings.RadioSettingGroup("close_call", "Close Call")
        for key, (name, choices) in _CHOICES.items():
            receiver.append(settings.RadioSetting(key, name, settings.RadioSettingValueList(
                choices.values(), current_index=list(choices).index(values[key]))))
        for key, name in _SWITCHES.items():
            group = close if key == "close_alert" else receiver
            group.append(settings.RadioSetting(key, name, settings.RadioSettingValueBoolean(values[key])))
        receiver.append(settings.RadioSetting("clock_offset", "Clock offset in minutes",
            settings.RadioSettingValueInteger(-1439, 1439, values["clock_offset"])))
        for index, name in enumerate(_CLOSE_GROUPS):
            close.append(settings.RadioSetting(f"close_{index}", name,
                settings.RadioSettingValueBoolean(values["close_groups"][index])))
        access = self._working.get("access")
        if access is None:
            return settings.RadioSettings(receiver, close)
        remote = settings.RadioSettingGroup("remote_access", "Remote Access")
        url = settings.RadioSettingValueString(0, _REMOTE_URL_LIMIT, access["remote_url"] or "", autopad=False)
        url.set_validate_callback(_validate_remote_url)
        url_setting = settings.RadioSetting("remote_url", "Remote URL", url)
        url_setting.__doc__ = "Only this URL can be used for remote access. Direct LAN access stays available."
        remote.append(url_setting)
        if self._base["access"]["remote_url"] is None:
            remote.append(settings.RadioSetting("block_other_remote", "Block remote access when URL is blank",
                settings.RadioSettingValueBoolean(access["remote_url"] == "")))
        configured = settings.RadioSettingValueBoolean(access["pin_configured"])
        remote.append(settings.RadioSetting("pin_configured", "PIN already set", configured))
        configured.set_mutable(False)
        pin = settings.RadioSetting(_NEW_PIN_SETTING, "New six-digit PIN", _NewPinValue(self._pending_pin, self))
        pin.__doc__ = "Leave blank to keep the current PIN. Upload applies a new PIN."
        remote.append(pin)
        return settings.RadioSettings(receiver, close, remote)

    def set_settings(self, edited):
        values = deepcopy(self._working["settings"])
        access = self._working.get("access")
        remote_url = access["remote_url"] if access is not None else None
        block_other_remote = remote_url == ""
        new_pin = self._pending_pin
        for item in edited.walk():
            key = item.get_name()
            if key in _CHOICES:
                values[key] = list(_CHOICES[key][1])[int(item.value)]
            elif key in _SWITCHES:
                values[key] = bool(item.value)
            elif key == "clock_offset":
                values[key] = int(item.value)
            elif key.startswith("close_"):
                values["close_groups"][int(key[6:])] = bool(item.value)
            elif access is not None:
                if key == "remote_url":
                    remote_url = str(item.value)
                elif key == "block_other_remote":
                    block_other_remote = bool(item.value)
                elif key == _NEW_PIN_SETTING:
                    new_pin = item.value.get_value()
        try:
            _setting_values(values)
            if access is not None:
                if remote_url is not None:
                    remote_url = _validate_remote_url(remote_url)
                if new_pin and not _PIN.fullmatch(new_pin):
                    raise ValueError("Enter a six-digit PIN or leave the field blank")
        except ValueError as error:
            raise errors.InvalidDataError(str(error)) from error
        self._working["settings"] = values
        if access is not None:
            if not remote_url:
                legacy = self._base["access"]["remote_url"] is None
                remote_url = None if legacy and not block_other_remote else ""
            access["remote_url"] = remote_url
            self._pending_pin = new_pin

    def _update_status(self, message, progress):
        status = chirp_common.Status()
        status.msg = message
        status.cur = progress
        self.status_fn(status)

    def _request(self, method, payload=None):
        match = _PORT.fullmatch(getattr(self.pipe, "port", "") or "")
        if match is None or int(match[1]) > 65535:
            raise errors.RadioError("Choose a custom socket://127.0.0.1:8766 port for SDr Scanner")
        data = b"" if payload is None else _encode(payload)
        if len(data) > _MAX_PAYLOAD:
            raise errors.RadioError("The scanner upload is too large")
        host = f"127.0.0.1:{match[1]}"
        headers = (f"{method} /api/chirp HTTP/1.1\r\nHost: {host}\r\n"
                   "X-SDR-Scanner-Clone: 1\r\nConnection: close\r\n")
        if payload is not None:
            headers += f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n"
        try:
            self.pipe.timeout = self.pipe.write_timeout = 8
            self.pipe.write(headers.encode("ascii") + b"\r\n" + data)
            response = bytearray()
            while not response.endswith(b"\r\n\r\n"):
                byte = self.pipe.read(1)
                if not byte or len(response) >= 8192:
                    raise ValueError("Incomplete scanner response")
                response.extend(byte)
            status_line, *fields = response.decode("ascii").split("\r\n")
            status = int(status_line.split()[1])
            lengths = [field.partition(":")[2] for field in fields
                       if field.lower().startswith("content-length:")]
            if len(lengths) != 1 or not 0 <= (size := int(lengths[0])) <= _MAX_PAYLOAD:
                raise ValueError("Invalid scanner response length")
            body = self.pipe.read(size)
            if len(body) != size:
                raise ValueError("Incomplete scanner response")
        except (OSError, TypeError, ValueError, IndexError) as error:
            raise errors.RadioError(f"Could not read from SDr Scanner: {error}") from error
        try:
            reply = json.loads(body)
        except (ValueError, RecursionError):
            reply = None
        if status != 200:
            message = reply.get("error") if isinstance(reply, dict) else None
            raise errors.RadioError(message or f"SDr Scanner returned HTTP {status}")
        try:
            return _validate_image(reply)
        except ValueError as error:
            raise errors.RadioError(str(error)) from error

    def sync_in(self):
        self._update_status("Downloading scanner channels", 0)
        self._base = self._request("GET")
        self._working = deepcopy(self._base)
        self._pending_pin = ""
        self._update_status("Download complete", 100)

    def _changes(self):
        changes = [{"slot": slot, "channel": new}
                   for slot, (old, new) in enumerate(zip(self._base["channels"], self._working["channels"]), 1)
                   if old != new]
        zones = {}
        for zone, entries in self._working["zones"].items():
            packed = [entry for entry in entries if entry is not None]
            if packed != self._base["zones"][zone]:
                zones[zone] = packed
        configuration = {key: value for key, value in self._working["settings"].items()
                         if self._base["settings"][key] != value}
        patch = {"version": 1, "revision": self._base["revision"], "changes": changes,
                 "zones": zones, "settings": configuration}
        if "access" in self._base:
            before, after = self._base["access"], self._working["access"]
            access = {}
            if after["remote_url"] != before["remote_url"]:
                access["remote_url"] = after["remote_url"]
            if self._pending_pin:
                access["new_pin"] = self._pending_pin
            if access:
                patch["access"] = {"revision": before["revision"], **access}
        return patch

    def sync_out(self):
        if self._base is None:
            raise errors.RadioError("Download from the scanner before uploading")
        self._update_status("Uploading scanner changes", 0)
        old_zones = self._working["zones"]
        self._base = self._request("POST", self._changes())
        self._working = deepcopy(self._base)
        self._pending_pin = ""
        if self._access_editors:
            wx = sys.modules.get("wx")
            if wx is not None:
                wx.CallAfter(self._reset_access_editors)
        # Keep the same CHIRP rows after the server removes empty zone rows.
        for zone, entries in old_zones.items():
            if [entry for entry in entries if entry is not None] == self._base["zones"][zone]:
                self._working["zones"][zone] = entries
        self._update_status("Upload complete", 100)

    def _reset_access_editors(self):
        for editor in tuple(self._access_editors):
            if editor:
                editor.reset_scanner_access_fields(self._working.get("access"))

    def save_mmap(self, filename):
        if self._base is None:
            raise errors.RadioError("Download from the scanner before saving an image")
        image = {"base": self._base, "working": self._working}
        self._mmap = memmap.MemoryMapBytes(_IMAGE_PREFIX + _encode(image))
        super().save_mmap(filename)

    def process_mmap(self):
        data = self._mmap.get_packed()
        if not data.startswith(_IMAGE_PREFIX) or len(data) > 2 * _MAX_PAYLOAD:
            raise errors.InvalidDataError("This is not an SDr Scanner image")
        try:
            image = json.loads(data[len(_IMAGE_PREFIX):])
            self._base = _validate_image(image["base"])
            self._working = _validate_image(image["working"], working=True)
            if (self._working["revision"] != self._base["revision"]
                    or self._working["reserved"] != self._base["reserved"]):
                raise ValueError("Image baseline mismatch")
            before, after = self._base.get("access"), self._working.get("access")
            if (before is None) != (after is None) or before is not None and (
                    before["revision"] != after["revision"] or before["pin_configured"] != after["pin_configured"]
                    or after["remote_url"] is None and before["remote_url"] is not None):
                raise ValueError("Remote access settings baseline mismatch")
        except (KeyError, TypeError, ValueError, RecursionError) as error:
            raise errors.InvalidDataError(f"Invalid SDr Scanner image: {error}") from error

    @classmethod
    def match_model(cls, filedata, filename):
        return filedata.startswith(_IMAGE_PREFIX)


def _optimise_chirp_gtk_zone_grids():
    # CHIRP has no per-driver grid hook. When loaded from its GTK GUI, give
    # large scanner zones column-wide styling and avoid sizing every cell.
    memedit = sys.modules.get("chirp.wxui.memedit")
    if memedit is None or not getattr(memedit, "WX_GTK", False):
        return
    grid_class = memedit.ChirpMemoryGrid
    if getattr(grid_class, "_sdr_scanner_optimised", False):
        return
    wx = memedit.wx

    class ScannerZoneGrid(grid_class):
        _sdr_scanner_optimised = True

        def __init__(self, parent):
            self._large_zone = (getattr(parent._radio, "_sdr_scanner_zone", False)
                                and parent._features.memory_bounds[1] >= 512)
            super().__init__(parent)

        def SetReadOnly(self, row, col, isReadOnly=True):
            if not self._large_zone:
                super().SetReadOnly(row, col, isReadOnly)

        def SetCellBackgroundColour(self, row, col, colour):
            if not self._large_zone:
                super().SetCellBackgroundColour(row, col, colour)

        def SetColAttr(self, col, attr):
            if self._large_zone and self.GetParent()._col_defs[col].name in _ZONE_READ_ONLY_COLUMNS:
                attr.SetReadOnly(True)
                appearance = getattr(wx.SystemSettings, "GetAppearance", None)
                colour = (wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT)
                          if appearance and appearance().IsDark() else wx.Colour(245, 245, 245))
                attr.SetBackgroundColour(colour)
            super().SetColAttr(col, attr)

        def AutoSizeColumns(self, setAsMin=True):
            if not self._large_zone:
                super().AutoSizeColumns(setAsMin)
                return
            for col in range(self.GetNumberCols()):
                if self.IsColShown(col):
                    width = max(self.GetTextExtent(line).width
                                for line in self.GetColLabelValue(col).split("\n"))
                    self.SetColSize(col, max(self.GetColSize(col), width + 24, 115))

        def AutoSizeRows(self, setAsMin=True):
            if not self._large_zone:
                super().AutoSizeRows(setAsMin)
                return
            font_height = self.GetDefaultCellFont().GetPixelSize().height
            self.SetDefaultRowSize(max(20, font_height + 8), True)

    memedit.ChirpMemoryGrid = ScannerZoneGrid


def _install_chirp_pin_editor():
    common = sys.modules.get("chirp.wxui.common")
    if common is None:
        return
    grid_class = common.ChirpSettingGrid
    if getattr(grid_class, "_sdr_scanner_pin_editor", False):
        return
    propgrid = common.wx.propgrid
    add_items = grid_class._add_items
    value_as_speech = getattr(grid_class, "_value_as_speech", None)

    def add_items_with_mask(self, group, parent=None):
        add_items(self, group, parent=parent)
        pin = self._settings.get(_NEW_PIN_SETTING)
        if pin is not None and hasattr(pin.value, "radio"):
            name = f"{_NEW_PIN_SETTING}{common.INDEX_CHAR}0"
            self.pg.SetPropertyAttribute(name, propgrid.PG_STRING_PASSWORD, True)
            self.pg.SetPropertyValue(name, pin.value.get_value())
            radio = pin.value.radio()
            if radio is not None:
                radio._access_editors.add(self)

    def property_speech(self, prop, raw_value=None):
        if prop.GetName() == f"{_NEW_PIN_SETTING}{common.INDEX_CHAR}0":
            value = prop.GetValue() if raw_value is None else raw_value
            return "PIN entered" if value else "Blank"
        return value_as_speech(self, prop, raw_value)

    def reset_scanner_access_fields(self, access):
        values = {_NEW_PIN_SETTING: ""}
        if access is not None:
            values.update(remote_url=access["remote_url"] or "",
                          block_other_remote=access["remote_url"] == "",
                          pin_configured=access["pin_configured"])
        for setting, value in values.items():
            prop = self.pg.GetProperty(f"{setting}{common.INDEX_CHAR}0")
            if prop is not None:
                prop.SetValue(value)
                prop.SetModifiedStatus(False)
        self._settings[_NEW_PIN_SETTING].value.set_value("")
        self._pending_value = None

    grid_class._add_items = add_items_with_mask
    if value_as_speech is not None:
        grid_class._value_as_speech = property_speech
    grid_class.reset_scanner_access_fields = reset_scanner_access_fields
    grid_class._sdr_scanner_pin_editor = True


_optimise_chirp_gtk_zone_grids()
_install_chirp_pin_editor()
