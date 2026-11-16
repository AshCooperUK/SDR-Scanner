#!/usr/bin/env python3
"""Test Nearby signal selection with synthetic signals or a receive-only RTL-SDR sweep."""

import argparse
from pathlib import Path
import sys
import time

import numpy as np


# Run directly from a source checkout with python3 tools/nearby_check.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bandplan import Band, PMR
from receiver import DIRECT_LIMIT, IQ_RATE, IQ_SAMPLES, TUNER_LIMIT, Receiver, ReceiverError
from v10 import NEARBY_PMR, NEARBY_TILE_HZ


PMR_CENTER = (PMR[0] + PMR[-1]) // 2 + 180_000
MINIMUM_RISE_DB = 6
HANDHELD_BANDS = (Band("VHF handheld", 136_000_000, 174_000_000),
                  Band("UHF handheld", 400_000_000, 480_000_000))


def stronger_signals(baseline, keyed):
    changed = [peak for hz, peak in keyed.items()
               if hz not in baseline or peak.strength_db - baseline[hz].strength_db >= MINIMUM_RISE_DB]
    return sorted(changed, key=lambda peak: peak.strength_db, reverse=True)


def print_signals(baseline, keyed):
    strongest = stronger_signals(baseline, keyed)
    if not strongest:
        print("No new or stronger signal was detected in the selected range.")
        return
    print("Strongest changes observed during the test")
    for peak in strongest[:8]:
        previous = baseline.get(peak.hz)
        change = (f"{peak.strength_db - previous.strength_db:+.1f} dB above baseline"
                  if previous else "not selected during baseline")
        print(f"  {peak.hz / 1e6:12.6f} MHz    {peak.level_dbfs:6.1f} dBFS    {change}")


def simulate():
    receiver = Receiver()
    timeline = np.arange(IQ_SAMPLES) / IQ_RATE
    weak_hz, strong_hz = PMR[1], PMR[0]
    weak = .035 * np.exp(2j * np.pi * (weak_hz - PMR_CENTER) * timeline)
    fm = (2_000 / 1_300) * np.sin(2 * np.pi * 1_300 * timeline)
    keyed = weak + .24 * np.exp(1j * (2 * np.pi * (strong_hz - PMR_CENTER) * timeline + fm))

    def measure(iq):
        return receiver._nearby_channel(iq, PMR_CENTER, NEARBY_PMR, PMR[0], PMR[-1], PMR, set(), -55)

    before, after = measure(weak), measure(keyed)
    if before is None or after is None or (before.hz, after.hz) != (weak_hz, strong_hz):
        raise RuntimeError(f"Simulated carrier check failed: baseline {before}, keyed {after}")
    print(f"Simulated baseline: {weak_hz / 1e6:.6f} MHz at {before.level_dbfs:.1f} dBFS.")
    print_signals({before.hz: before}, {after.hz: after})
    print("PASS: Nearby selected the stronger of the two signals. No SDR was opened.")


def acquire(receiver, bands, seconds, squelch):
    tiles = [(band, low, min(low + NEARBY_TILE_HZ, band.upper))
             for band in bands if band != NEARBY_PMR
             for low in range(band.lower, band.upper, NEARBY_TILE_HZ)]
    if NEARBY_PMR in bands:
        tiles.insert(0, (NEARBY_PMR, PMR[0], PMR[-1]))
    peaks = {}
    count = 0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        sweep, index = divmod(count, len(tiles))
        band, low, high = tiles[index]
        center = (low + high) // 2 + (60_000 if sweep % 2 == 0 else -60_000)
        exact = PMR if band == NEARBY_PMR else ()
        center = PMR_CENTER if exact else min(TUNER_LIMIT, max(DIRECT_LIMIT, center))
        peak = receiver.nearby_peak(center, band, low, high, exact, set(), squelch)
        if peak is not None:
            previous = peaks.get(peak.hz)
            if previous is None or peak.strength_db > previous.strength_db:
                peaks[peak.hz] = peak
        count += 1
    print(f"Measured {count} capture windows ({min(count, len(tiles))} of {len(tiles)} distinct windows).")
    return peaks


def live(args, bands):
    receiver = Receiver(args.device)
    try:
        receiver.open()
        if args.gain is not None:
            receiver.configure({"rf_gain": args.gain})
        print("Listening on " + ", ".join(
            f"{band.lower / 1e6:.6f}–{band.upper / 1e6:.6f} MHz" for band in bands), flush=True)
        input(f"Leave the handheld idle and press Enter to capture a {args.seconds:g}s baseline. ")
        baseline = acquire(receiver, bands, args.seconds, args.squelch)
        input(f"Press Enter to start the {args.seconds:g}s test, then key the handheld on your test channel. ")
        print("Capturing the test now.", flush=True)
        keyed = acquire(receiver, bands, args.seconds, args.squelch)
        print_signals(baseline, keyed)
    finally:
        receiver.close()


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=("Live mode needs an RTL-SDR available to open. Use a spare dongle or release the app's tuner first. "
                "A single tuner checks wide ranges in pieces and can miss a short burst. A narrow range is faster. "
                "This compares the strongest frequencies measured in each window. It does not identify or decode the mode."),
    )
    parser.add_argument("--live", action="store_true", help="measure an RTL-SDR instead of the simulated carriers")
    ranges = parser.add_mutually_exclusive_group()
    ranges.add_argument("--band", choices=("pmr", "handheld"), default="pmr",
                        help="live sweep range: PMR446 (default) or 136–174 and 400–480 MHz")
    ranges.add_argument("--range", nargs=2, type=float, metavar=("LOW_MHZ", "HIGH_MHZ"),
                        help="custom live range, using the 6.25 kHz frequency grid")
    parser.add_argument("--seconds", type=float, default=8,
                        help="maximum duration of each live measurement phase (default 8 seconds)")
    parser.add_argument("--device", type=int, default=0, help="zero-based RTL-SDR device index (default 0)")
    parser.add_argument("--squelch", type=float, default=-55, help="live signal threshold in dBFS (default −55)")
    parser.add_argument("--gain", type=int, choices=range(101), metavar="PERCENT",
                        help="set live tuner gain as 0–100%% of its range")
    args = parser.parse_args()
    if not (0 < args.seconds <= 120) or args.device < 0 or not (-150 <= args.squelch <= 0):
        parser.error("use 0–120 seconds, a non-negative device index and squelch from −150 to 0 dBFS")
    bands = (NEARBY_PMR, *HANDHELD_BANDS) if args.band == "handheld" else (NEARBY_PMR,)
    if args.range:
        low, high = args.range
        if not (DIRECT_LIMIT / 1e6 <= low < high <= TUNER_LIMIT / 1e6):
            parser.error("custom range must be in ascending order between 25 and 1750 MHz")
        low, high = (round(mhz * 1e6) for mhz in (low, high))
        if low == high:
            parser.error("custom range edges must be at least 1 Hz apart")
        bands = (Band("Custom", low, high),)
    try:
        if args.live:
            live(args, bands)
        else:
            simulate()
    except (ReceiverError, EOFError) as error:
        parser.exit(1, f"Nearby test ended: {error}\n")
    except KeyboardInterrupt:
        parser.exit(130, "\nNearby test cancelled.\n")


if __name__ == "__main__":
    main()
