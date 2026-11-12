"""Decode the legacy FM Radio Data System from a broadcast multiplex.

The physical layer and block checkwords are specified in NRSC-4 / IEC 62106.
https://www.nrscstandards.org/standards-and-guidelines/documents/archive/nrsc-4-1998.pdf
"""

from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from itertools import combinations
from logging import getLogger
from math import gcd
from time import monotonic

import numpy as np
from scipy import signal


PILOT_HZ = 19_000
SUBCARRIER_HZ = 3 * PILOT_HZ
SYMBOL_RATE = SUBCARRIER_HZ / 48
DECIMATION = 16
BLOCK_BITS = 26
BLOCK_MASK = (1 << BLOCK_BITS) - 1
GROUP_BITS = 4 * BLOCK_BITS
GROUP_MASK = (1 << GROUP_BITS) - 1
CHECKWORD_BITS = 10
GENERATOR = 0x5B9
OFFSET_A, OFFSET_B, OFFSET_C, OFFSET_C_PRIME, OFFSET_D = (
    0x0FC, 0x198, 0x168, 0x350, 0x1B4,
)
SYNC_TIMEOUT = 2.0
DATA_TIMEOUT = 8.0
WEAK_WINDOW = 12
WEAK_ACQUIRE = 4
WEAK_INTERVAL = 2
PILOT_INTERVAL = 0.05
# The G0 character repertoire is specified in IEC 62106-4. Each row is
# one high-nibble range, starting at 0x80.
G0_EXTENDED = (
    "áàéèíìóòúùÑÇŞβ¡Ĳ"
    "âäêëîïôöûüñçşǧıĳ"
    "ªα©‰Ǧěňőπ€£$←↑→↓"
    "º¹²³±İńűµ¿÷°¼½¾§"
    "ÁÀÉÈÍÌÓÒÚÙŘČŠŽÐĿ"
    "ÂÄÊËÎÏÔÖÛÜřčšžđŀ"
    "ÃÅÆŒŷÝÕØÞŊŔĆŚŹŦð"
    "ãåæœŵýõøþŋŕćśźŧ "
)
G0_BASIC_EXCEPTIONS = {0x24: "¤", 0x5E: "―", 0x60: "‖", 0x7E: "¯"}
G0_ALPHABET = (" " * 32 + "".join(G0_BASIC_EXCEPTIONS.get(byte, chr(byte))
                                 for byte in range(32, 127)) + " " + G0_EXTENDED)


def _remainder(block):
    for shift in range(BLOCK_BITS - 1, CHECKWORD_BITS - 1, -1):
        if block & (1 << shift):
            block ^= GENERATOR << (shift - CHECKWORD_BITS)
    return block


SINGLE_BIT_ERRORS = {_remainder(1 << bit): 1 << bit for bit in range(BLOCK_BITS)}
BIT_WEIGHTS = 1 << np.arange(BLOCK_BITS - 1, -1, -1, dtype=np.uint32)
_WEAK_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rds")
_LOG = getLogger(__name__)


def _text(data):
    return "".join(G0_ALPHABET[byte] for byte in data).strip()


class _Groups:
    """Align to checked groups and keep their block timing through errors."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.reset_sync()
        self.pi = None
        self.ps = ""
        self.text = ""
        self.last_valid = None
        self._ps_segments = {}
        self._text_segments = {}
        self._text_kind = None

    def reset_sync(self):
        self._shift = 0
        self._bit_count = 0
        self._next_group = None
        self._bad_groups = 0

    @staticmethod
    def _block(word, remainder, offset):
        difference = remainder ^ offset
        if difference == 0:
            return word >> CHECKWORD_BITS, True
        error = SINGLE_BIT_ERRORS.get(difference)
        return (word ^ error) >> CHECKWORD_BITS if error is not None else None, False

    def push(self, bits):
        for bit in bits:
            self._shift = ((self._shift << 1) | int(bit)) & GROUP_MASK
            self._bit_count += 1
            if self._bit_count < GROUP_BITS:
                continue
            if self._next_group is not None and self._bit_count < self._next_group:
                continue
            self._read_group()

    def _read_group(self):
        a, b, c, d = ((self._shift >> shift) & BLOCK_MASK
                      for shift in (3 * BLOCK_BITS, 2 * BLOCK_BITS, BLOCK_BITS, 0))
        crc_a, crc_b, crc_d = _remainder(a), _remainder(b), _remainder(d)
        locked = self._next_group is not None
        if not locked and sum((crc_a == OFFSET_A, crc_b == OFFSET_B, crc_d == OFFSET_D)) < 2:
            return
        pi, clean_a = self._block(a, crc_a, OFFSET_A)
        header, clean_b = self._block(b, crc_b, OFFSET_B)
        block_d, clean_d = self._block(d, crc_d, OFFSET_D)
        crc_c = _remainder(c)
        if header is None:
            block_c, clean_c = None, crc_c in (OFFSET_C, OFFSET_C_PRIME)
        else:
            offset_c = OFFSET_C_PRIME if header & 0x800 else OFFSET_C
            block_c, clean_c = self._block(c, crc_c, offset_c)
        clean_blocks = sum((clean_a, clean_b, clean_c, clean_d))
        if not locked and clean_blocks < 3:
            return
        self._next_group = self._bit_count + GROUP_BITS
        self._bad_groups = self._bad_groups + 1 if clean_blocks < 2 else 0
        if self._bad_groups >= 8:
            self._next_group = None
            self._bad_groups = 0
        if clean_blocks < 2 or header is None:
            return
        if not clean_a:
            if header & 0x800 and clean_c:
                pi = block_c
            elif (locked and clean_b and clean_d and self.pi is not None
                  and pi in (None, self.pi)):
                # A damaged A block can be ignored on an already identified
                # station when both the header and its payload check cleanly.
                pi = self.pi
            elif pi is None or pi != self.pi:
                return
        if header & 0x800 and not clean_c:
            block_c = None
        if not clean_b and not clean_d:
            block_d = None
        self._decode(pi, header, block_c, block_d)

    def _decode(self, pi, header, block_c, block_d):
        if header & 0x800 and block_c is not None and block_c != pi:
            return
        if pi != self.pi:
            self.pi = pi
            self.ps = self.text = ""
            self._ps_segments.clear()
            self._text_segments.clear()
            self._text_kind = None
        self.last_valid = monotonic()
        group = header >> 12
        if group == 0 and block_d is not None:
            index = header & 3
            pair = block_d.to_bytes(2, "big")
            if index in self._ps_segments and self._ps_segments[index] != pair:
                self._ps_segments.clear()
            self._ps_segments[index] = pair
            if len(self._ps_segments) == 4:
                self.ps = _text(b"".join(self._ps_segments[index] for index in range(4)))
        elif group == 2 and block_d is not None and (header & 0x800 or block_c is not None):
            self._decode_radio_text(header, block_c, block_d)

    def _decode_radio_text(self, header, block_c, block_d):
        short_group = bool(header & 0x800)
        text_kind = (short_group, bool(header & 0x10))
        if self._text_kind != text_kind:
            self._text_kind = text_kind
            self._text_segments.clear()
            self.text = ""
        index = header & 15
        segment = block_d.to_bytes(2, "big")
        if not short_group:
            segment = block_c.to_bytes(2, "big") + segment
        self._text_segments[index] = segment

        # The carriage return ends variable-length RadioText. Require all
        # segments before it so a station can't display a mixture of messages.
        assembled = bytearray()
        for index in range(16):
            if index not in self._text_segments:
                return
            assembled.extend(self._text_segments[index])
            if 13 in assembled:
                self.text = _text(assembled.split(b"\r", 1)[0])
                return
        self.text = _text(assembled)

    def status(self):
        if self.last_valid is None or monotonic() - self.last_valid > DATA_TIMEOUT:
            return None
        return {"pi": f"{self.pi:04X}", "ps": self.ps, "text": self.text}


@lru_cache(maxsize=1)
def _remainder_tables():
    return np.array([[_remainder((byte << shift) & BLOCK_MASK) for byte in range(256)]
                     for shift in range(0, BLOCK_BITS, 8)], dtype=np.uint16)


def _block_remainders(words):
    tables = _remainder_tables()
    return (tables[0, words & 255] ^ tables[1, (words >> 8) & 255]
            ^ tables[2, (words >> 16) & 255] ^ tables[3, words >> 24])


@lru_cache(maxsize=1)
def _symbol_errors():
    # The differential decoder uses 27 symbols for a 26-bit block. Changing
    # one symbol affects both adjacent bits, except at the two block edges.
    symbol_count = BLOCK_BITS + 1
    max_errors = 4
    flips = [((1 << (BLOCK_BITS - i - 1)) if i < BLOCK_BITS else 0)
             | ((1 << (BLOCK_BITS - i)) if i else 0) for i in range(symbol_count)]
    syndromes = [_remainder(mask) for mask in flips]
    patterns = [[] for _ in range(1 << CHECKWORD_BITS)]
    for count in range(max_errors + 1):
        for positions in combinations(range(symbol_count), count):
            mask = syndrome = 0
            for index in positions:
                mask ^= flips[index]
                syndrome ^= syndromes[index]
            patterns[syndrome].append((mask, positions))
    width = max(map(len, patterns))
    masks = np.zeros((len(patterns), width), dtype=np.uint32)
    positions = np.full((len(patterns), width, max_errors), symbol_count + 1, dtype=np.intp)
    for syndrome, candidates in enumerate(patterns):
        for index, (mask, indexes) in enumerate(candidates):
            masks[syndrome, index] = mask
            positions[syndrome, index] = indexes + (symbol_count,) * (max_errors - len(indexes))
    return masks, positions


def _symbol_words(symbols):
    signs = symbols < 0
    return (signs[:, 1:] ^ signs[:, :-1]) @ BIT_WEIGHTS


def _soft_blocks(symbols, offset):
    words = _symbol_words(symbols)
    syndromes = _block_remainders(words) ^ offset
    masks, positions = _symbol_errors()
    # Flipping a low-amplitude symbol is cheaper than flipping one with
    # strong evidence. Unused entries have infinite cost.
    weights = np.abs(symbols)
    padded = np.pad(weights, ((0, 0), (0, 2)), constant_values=0)
    padded[:, -1] = np.inf
    row = np.arange(len(words))
    costs = padded[row[:, None, None], positions[syndromes]].sum(axis=2)
    best = np.argmin(costs, axis=1)
    total_weight = np.maximum(weights.sum(axis=1), 1e-12)
    cost = costs[row, best] / total_weight
    alternative = np.partition(costs, 1, axis=1)[:, 1]
    margin = alternative / total_weight - cost
    accepted = (cost <= 0.065) & (margin >= 0.005)
    return (words ^ masks[syndromes, best]) >> CHECKWORD_BITS, cost, accepted


def _coherent_frequency(samples, interval, max_hz):
    if len(samples) < 8 or not np.any(samples):
        return None
    time = interval * np.arange(len(samples))
    phase = np.unwrap(np.angle(samples))
    slope, start = np.polyfit(time, phase, 1)
    frequency = slope / (2 * np.pi)
    coherence = abs(np.exp(1j * (phase - slope * time - start)).mean())
    return frequency if coherence >= 0.90 and abs(frequency) <= max_hz else None


def _weak_symbols(samples, rate, pilot, pilot_interval):
    offset = _coherent_frequency(pilot, pilot_interval, 5)
    if offset is None:
        # A mono broadcaster can still carry RDS. Squaring the BPSK signal
        # removes its sign modulation and lets us estimate subcarrier drift.
        interval = min(pilot_interval, 0.025)
        size = round(rate * interval)
        blocks = samples[:len(samples) // size * size].reshape(-1, size)
        frequency = _coherent_frequency((blocks ** 2).mean(axis=1), size / rate, 15)
        offset = 0 if frequency is None else frequency / 6
    if offset:
        samples = samples * np.exp((-6j * np.pi * offset / rate) * np.arange(len(samples)))
    period = rate * 16 / (PILOT_HZ + offset)
    prefix = np.concatenate(([0j], np.cumsum(samples, dtype=np.complex128)))
    starts = period * np.arange(2, int(len(samples) / period) - 3)
    grid = starts[None, :] + period * (np.arange(16)[:, None] / 16 + 0.05)
    integral = RdsDecoder._integral
    # Use the middle 80 percent of each half-symbol to reduce contamination
    # by the neighbouring bit at less reliable symbol boundaries.
    symbols = (integral(prefix, grid + 0.40 * period) - integral(prefix, grid)
               - integral(prefix, grid + 0.90 * period) + integral(prefix, grid + 0.50 * period))
    return symbols, period


def _weak_alignments(symbols, phase_angles):
    candidates = []
    for phase, (samples, angle) in enumerate(zip(symbols, phase_angles)):
        projected = np.real(samples * np.exp(-1j * angle))
        for bits in ((projected[1:] * projected[:-1]) < 0,
                     np.real(samples[1:] * samples[:-1].conj()) < 0):
            words = np.zeros(len(bits) - BLOCK_BITS + 1, dtype=np.uint32)
            for index in range(BLOCK_BITS):
                words = (words << 1) | bits[index:index + len(words)]
            remainders = _block_remainders(words)
            hits = np.flatnonzero((remainders == OFFSET_A) & ((words >> CHECKWORD_BITS) != 0))
            identities = (words[hits] >> CHECKWORD_BITS) * GROUP_BITS + hits % GROUP_BITS
            for identity, count in Counter(identities.tolist()).most_common():
                if count < 3:
                    break
                pi, alignment = divmod(identity, GROUP_BITS)
                starts = np.arange(alignment, len(words) - 3 * BLOCK_BITS, GROUP_BITS)
                other_checks = ((remainders[starts + BLOCK_BITS] == OFFSET_B)
                                | (remainders[starts + 3 * BLOCK_BITS] == OFFSET_D))
                if np.count_nonzero(other_checks) < 3:
                    continue
                exact = starts[(remainders[starts] == OFFSET_A)
                               & ((words[starts] >> CHECKWORD_BITS) == pi)]
                if len(exact) >= 3:
                    candidates.append((count + int(np.count_nonzero(other_checks)),
                                       phase, pi, alignment, exact[-1]))
    return sorted(candidates, reverse=True)


def _weak_name_votes(symbols, phase_angle, starts, pi, pi_signs):
    group = symbols[starts[:, None] + np.arange(GROUP_BITS + 1)]
    pi_symbols = group[:, :BLOCK_BITS + 1]
    pi_match = pi_symbols @ pi_signs
    credible = np.abs(pi_match) >= 0.65 * np.abs(pi_symbols).sum(axis=1)
    group, pi_match, starts = group[credible], pi_match[credible], starts[credible]
    for angle in (phase_angle, np.angle(pi_match)[:, None]):
        projected = np.real(group * np.exp(-1j * angle))
        pi_symbols = projected[:, :BLOCK_BITS + 1]
        credible = np.abs(pi_symbols @ pi_signs) >= 0.72 * np.abs(pi_symbols).sum(axis=1)
        if not np.any(credible):
            continue
        projected, matched_starts = projected[credible], starts[credible]
        b, b_cost, b_ok = _soft_blocks(projected[:, BLOCK_BITS:2 * BLOCK_BITS + 1], OFFSET_B)
        d, d_cost, d_ok = _soft_blocks(projected[:, 3 * BLOCK_BITS:], OFFSET_D)
        c = _symbol_words(projected[:, 2 * BLOCK_BITS:3 * BLOCK_BITS + 1])
        conflicting_pi = (((b & 0x800) != 0) & (_block_remainders(c) == OFFSET_C_PRIME)
                          & ((c >> CHECKWORD_BITS) != pi))
        # Only PS groups can contribute a name. Control bytes are reserved
        # in PS, so never use them as evidence for a corrected name.
        valid = b_ok & d_ok & ((b >> 12) == 0) & (b_cost + d_cost <= 0.12) & ~conflicting_pi
        for start, header, value in zip(matched_starts[valid], b[valid], d[valid]):
            pair = int(value).to_bytes(2, "big")
            if all(byte >= 32 and byte != 127 for byte in pair):
                yield int(header & 3), pair, int(start)


def _weak_decode(chunks, rate, pilot, pilot_interval):
    samples = np.concatenate(chunks)
    symbols, period = _weak_symbols(samples, rate, pilot, pilot_interval)
    angles = np.angle(np.mean(symbols ** 2, axis=1)) / 2
    alignments = _weak_alignments(symbols, angles)
    if not alignments:
        return None
    _, _, pi, alignment, _ = alignments[0]
    phases = {}
    for _, phase, candidate_pi, candidate_alignment, last_pi in alignments[:8]:
        if (candidate_pi, candidate_alignment) == (pi, alignment):
            phases[phase] = max(last_pi, phases.get(phase, 0))
    pi_word = pi << CHECKWORD_BITS
    pi_word |= _remainder(pi_word) ^ OFFSET_A
    pi_signs = np.concatenate(([1], np.cumprod(np.where(pi_word & BIT_WEIGHTS, -1, 1))))
    votes = [{} for _ in range(4)]
    starts = np.arange(alignment, symbols.shape[1] - GROUP_BITS, GROUP_BITS)
    for phase in phases:
        for index, pair, start in _weak_name_votes(symbols[phase], angles[phase], starts, pi, pi_signs):
            # A reception at several trial phases is still one RF group.
            votes[index].setdefault(pair, set()).add(start)
    name = bytearray()
    first_seen, last_seen = [], []
    for segment in votes:
        pairs = sorted(segment, key=lambda pair: len(segment[pair]), reverse=True)
        if (not pairs or len(segment[pairs[0]]) < 3
                or (len(pairs) > 1 and len(segment[pairs[0]]) < len(segment[pairs[1]]) + 2)):
            name.clear()
            break
        name.extend(pairs[0])
        first_seen.append(min(segment[pairs[0]]))
        last_seen.append(max(segment[pairs[0]]))
    # Avoid joining old and new parts of a station name if the station
    # changes its PS during the acquisition window.
    if name and max(first_seen) > min(last_seen):
        name.clear()
    # Keep age tied to the received evidence, even when windows overlap or
    # a background calculation completes after the RF signal has vanished.
    def age(index, bits):
        return max(0, (len(samples) - (index + bits + 3) * period) / rate)

    return (pi, _text(name), age(max(phases.values()), BLOCK_BITS),
            age(min(last_seen, default=0), GROUP_BITS))


class _WeakRds:
    """Run a bounded, CRC-checked acquisition pass away from the audio path."""

    def __init__(self, rate):
        self._rate = rate / DECIMATION
        self._window_samples = round(self._rate * WEAK_WINDOW)
        self._pilot_size = max(1, round(rate * PILOT_INTERVAL))
        self._pilot_interval = self._pilot_size / rate
        self._pilot_oscillator = np.exp(
            (-2j * np.pi * PILOT_HZ / rate) * np.arange(self._pilot_size)
        ).astype(np.complex64)
        self._pilot_step = np.exp(-2j * np.pi * PILOT_HZ * self._pilot_interval)
        self._future = None
        self.reset()

    def reset(self, keep_metadata=False):
        if self._future is not None:
            self._future.cancel()
        self._future = None
        self._samples = deque()
        self._length = 0
        self._until_attempt = round(self._rate * WEAK_ACQUIRE)
        self._pilot = deque(maxlen=round(WEAK_WINDOW / self._pilot_interval))
        self._pilot_count = 0
        self._pilot_sum = 0j
        self._pilot_rotation = 1 + 0j
        if not keep_metadata:
            self._pi = None
            self._ps = ""
            self._pi_at = self._ps_at = 0.0

    def suspend(self):
        if self._length:
            self.reset()

    def feed(self, composite, samples):
        self._samples.append(samples)
        self._length += len(samples)
        while self._length - len(self._samples[0]) >= self._window_samples:
            self._length -= len(self._samples.popleft())
        if self._length > self._window_samples:
            excess = self._length - self._window_samples
            self._samples[0] = self._samples[0][excess:].copy()
            self._length = self._window_samples
        start = 0
        while start < len(composite):
            count = min(len(composite) - start, self._pilot_size - self._pilot_count)
            self._pilot_sum += composite[start:start + count] @ self._pilot_oscillator[
                self._pilot_count:self._pilot_count + count]
            self._pilot_count += count
            start += count
            if self._pilot_count == self._pilot_size:
                self._pilot.append(self._pilot_sum * self._pilot_rotation)
                self._pilot_count = 0
                self._pilot_sum = 0j
                self._pilot_rotation *= self._pilot_step
        self._until_attempt -= len(samples)
        self._collect()
        if self._until_attempt <= 0 and self._future is None:
            self._requested_at = monotonic()
            self._future = _WEAK_EXECUTOR.submit(_weak_decode, tuple(self._samples), self._rate,
                                                np.array(self._pilot), self._pilot_interval)
            self._until_attempt = round(self._rate * WEAK_INTERVAL)

    def _collect(self):
        if self._future is None or not self._future.done():
            return
        future, self._future = self._future, None
        try:
            result = future.result()
        except Exception:
            # Failure of the optional analysis must not interrupt reception.
            _LOG.exception("Weak-signal RDS analysis failed")
            self._until_attempt = float("inf")
            return
        if result is None:
            return
        pi, ps, pi_age, ps_age = result
        pi_at = self._requested_at - pi_age
        if self._pi != pi:
            if pi_at <= self._pi_at or monotonic() - pi_at > DATA_TIMEOUT:
                return
            self._pi = pi
            self._ps = ""
            self._pi_at = self._ps_at = 0.0
        self._pi_at = max(self._pi_at, pi_at)
        if ps and self._requested_at - ps_age > self._ps_at:
            self._ps = ps
            self._ps_at = self._requested_at - ps_age

    def status(self):
        self._collect()
        now = monotonic()
        if self._pi is None or now - self._pi_at > DATA_TIMEOUT:
            return None
        return {"pi": f"{self._pi:04X}",
                "ps": self._ps if now - self._ps_at <= DATA_TIMEOUT else "", "text": ""}


class RdsDecoder:
    """Recover clock and differential biphase bits from real composite samples."""

    def __init__(self, rate):
        self._period = rate / DECIMATION / SYMBOL_RATE
        self._oscillator_period = rate // gcd(rate, SUBCARRIER_HZ)
        self._oscillator = np.exp(
            -2j * np.pi * SUBCARRIER_HZ * np.arange(self._oscillator_period) / rate
        ).astype(np.complex64)
        # Approximates the RDS cosine shaping and rejects the stereo difference
        # signal below 53 kHz before downsampling the translated subcarrier.
        frequencies = [0, 500, 1000, 1500, 2000, 2375, 3500, rate / 2]
        gains = [np.cos(np.pi * hz / (4 * SYMBOL_RATE)) if hz < 2375 else 0
                 for hz in frequencies]
        self._filter = signal.firwin2(1025, frequencies, gains, fs=rate).astype(np.float32)
        self._history_size = len(self._filter) - 1
        self._groups = _Groups()
        self._weak = _WeakRds(rate)
        self.reset()

    def reset(self, keep_metadata=False):
        self._carrier_sample = 0
        self._history = np.zeros(self._history_size, dtype=np.complex64)
        self._composite_tail = np.empty(0, dtype=np.float32)
        self._samples = np.empty(0, dtype=np.complex64)
        self._next_symbol = None
        self._previous_symbol = None
        self._lock_started = None
        self._weak.reset(keep_metadata)
        if keep_metadata:
            self._groups.reset_sync()
        else:
            self._groups.reset()

    def status(self):
        fast = self._groups.status()
        if fast and fast["ps"]:
            return fast
        weak = self._weak.status()
        if fast and weak and fast["pi"] == weak["pi"]:
            fast["ps"] = weak["ps"]
        return fast or weak

    def feed(self, composite):
        composite = np.concatenate((self._composite_tail, composite))
        size = len(composite) // DECIMATION * DECIMATION
        self._composite_tail = composite[size:]
        if size == 0:
            return
        oscillator_index = (self._carrier_sample + np.arange(size)) % self._oscillator_period
        self._carrier_sample = (self._carrier_sample + size) % self._oscillator_period
        shifted = composite[:size] * self._oscillator[oscillator_index]
        samples = np.concatenate((self._history, shifted))
        filtered = signal.upfirdn(self._filter, samples, down=DECIMATION)
        start = self._history_size // DECIMATION
        self._history = samples[-self._history_size:]
        baseband = filtered[start:start + size // DECIMATION]
        self._samples = np.concatenate((self._samples, baseband))
        self._recover_bits()
        fast = self._groups.status()
        if not fast or not fast["ps"]:
            self._weak.feed(composite[:size], baseband)
        else:
            self._weak.suspend()

    @staticmethod
    def _integral(prefix, positions):
        index = positions.astype(int)
        fraction = positions - index
        return prefix[index] + fraction * (prefix[index + 1] - prefix[index])

    def _symbols(self, prefix, starts):
        middle = self._integral(prefix, starts + self._period / 2)
        return 2 * middle - self._integral(prefix, starts) - self._integral(prefix, starts + self._period)

    def _recover_bits(self):
        acquisition_symbols = 140
        now = monotonic()
        if self._next_symbol is not None:
            last_lock = max(self._groups.last_valid or 0, self._lock_started)
            if now - last_lock > SYNC_TIMEOUT:
                self._next_symbol = None
                self._previous_symbol = None
                self._groups.reset_sync()
        if self._next_symbol is None and len(self._samples) < self._period * (acquisition_symbols + 3):
            return

        prefix = np.concatenate(([0j], np.cumsum(self._samples, dtype=np.complex128)))
        if self._next_symbol is None:
            # Try one symbol period of candidate starting phases. A matched
            # biphase template has maximum energy at the data clock phase.
            phases = self._period * np.arange(16) / 16
            grid = phases[:, None] + self._period * np.arange(acquisition_symbols)
            energy = np.mean(np.abs(self._symbols(prefix, grid)) ** 2, axis=1)
            self._next_symbol = float(phases[np.argmax(energy)])
            self._lock_started = now

        count = int((len(self._samples) - 2 - self._next_symbol) // self._period)
        if count <= 0:
            return
        starts = self._next_symbol + self._period * np.arange(count)
        symbols = self._symbols(prefix, starts)
        if self._previous_symbol is None:
            previous = symbols[:-1]
            current = symbols[1:]
        else:
            previous = np.concatenate(([self._previous_symbol], symbols[:-1]))
            current = symbols
        self._groups.push(np.real(current * np.conjugate(previous)) < 0)
        self._previous_symbol = symbols[-1]

        # A small early/late correction tracks crystal drift without changing
        # the number of decoded symbols.
        margin = min(0.35, self._next_symbol)
        correction = 0.0
        if margin > 0:
            early = np.mean(np.abs(self._symbols(prefix, starts - margin)) ** 2)
            late = np.mean(np.abs(self._symbols(prefix, starts + margin)) ** 2)
            if early + late:
                correction = float(np.clip(3 * (late - early) / (late + early), -0.10, 0.10))
        self._next_symbol += count * self._period + correction
        consumed = max(0, int(self._next_symbol) - 1)
        self._samples = self._samples[consumed:]
        self._next_symbol -= consumed
