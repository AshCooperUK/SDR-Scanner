"""Receiver grids and factory bank data for the photographed scanner."""

from dataclasses import dataclass, replace
from typing import NamedTuple


class Channel(NamedTuple):
    hz: int
    modulation: str
    bank: str = ""
    slot: int | None = None
    preset_number: int | None = None


@dataclass(frozen=True)
class Band:
    label: str
    lower: int
    upper: int
    step: int = 6_250
    modulation: str = "FM"

    def count(self, step: int) -> int:
        if step == AIR_STEP:
            return (self.upper - self.lower) * 3 // 25_000 + 1
        return (self.upper - self.lower) // step + 1

    def frequency(self, index: int, step: int) -> int:
        if step == AIR_STEP:
            # The "8.33 kHz" setting is exactly 25,000 Hz divided by three.
            return self.lower + (index * 25_000 + 1) // 3
        return self.lower + index * step

    def nearest(self, hz: int, step: int) -> int:
        spacing = 25_000 / 3 if step == AIR_STEP else step
        index = round((hz - self.lower) / spacing)
        return self.frequency(max(0, min(index, self.count(step) - 1)), step)


AIR_STEP = 8_333
AIR_INDEX = 6
BANDS = (
    Band("25:30", 25_000_000, 29_995_000, 5_000),
    Band("30:50", 30_000_000, 49_995_000, 5_000),
    Band("50:80", 50_000_000, 79_995_000, 5_000),
    Band("80:83", 80_000_000, 82_995_000, 5_000),
    Band("83:84", 83_000_000, 84_010_000, 5_000),
    Band("84:87", 84_015_000, 87_295_000, 5_000),
    Band("108:137", 108_000_000, 136_991_667, AIR_STEP, "AM"),
    Band("137:138", 137_000_000, 137_993_750),
    Band("138:144", 138_000_000, 143_993_750),
    Band("144:146", 144_000_000, 145_993_750),
    Band("146:156", 146_000_000, 155_993_750),
    Band("156:157", 156_000_000, 157_431_250),
    Band("157:158", 157_437_500, 157_993_750),
    Band("158:160", 158_000_000, 160_593_750),
    Band("160:162", 160_600_000, 162_025_000),
    Band("162:162", 162_031_250, 162_593_750),
    Band("162:174", 162_600_000, 174_000_000),
    Band("406:440", 406_000_000, 439_993_750),
    Band("440:450", 440_000_000, 449_993_750),
    Band("450:466", 450_000_000, 465_993_750),
    Band("466:470", 466_000_000, 469_993_750),
    Band("470:512", 470_000_000, 512_000_000),
    Band("806:960", 806_000_000, 960_000_000, 12_500),
)

GERMAN_CHANGES = {
    0: {"modulation": "AM"},
    5: {"step": 20_000},
    7: {"upper": 137_995_000, "step": 5_000},
    8: {"upper": 143_995_000, "step": 5_000},
    9: {"upper": 145_987_500, "step": 12_500},
    10: {"upper": 155_990_000, "step": 10_000},
    11: {"upper": 157_425_000, "step": 12_500},
    12: {"upper": 157_987_500, "step": 12_500},
    13: {"upper": 160_587_500, "step": 12_500},
    14: {"step": 12_500},
    15: {"lower": 162_030_000, "upper": 162_590_000, "step": 10_000},
    16: {"step": 10_000},
    19: {"upper": 465_990_000, "step": 10_000},
    20: {"upper": 469_990_000, "step": 10_000},
}
GERMAN_BANDS = tuple(replace(band, **GERMAN_CHANGES.get(index, {}))
                     for index, band in enumerate(BANDS))

BANKS = ("PVT", "FAV", "PMR", "AIR", "MRN", "CBAM", "CBFM")
BANK_KEYS = {
    "private": ("PVT",),
    "fav": ("FAV",),
    "pmr": ("PMR",),
    "air": ("AIR", "MRN"),
    "cb": ("CBAM", "CBFM"),
}
WRITABLE = {"private": "PVT", "fav": "FAV"}
FULL_LOCKOUT = {"PVT", "FAV", "PMR", "MRN"}
SLOTS = 100
GENERAL_STEPS = (0, 5_000, 6_250, 10_000, 12_500)
AIR_STEPS = (AIR_STEP, 12_500)

CLOSE_GROUPS = (
    ("b1 Lo", 25_000_000, 87_295_000),
    ("b2 Air", 108_000_000, 136_991_667),
    ("b3 Hi", 137_000_000, 174_000_000),
    ("b4 UHF", 406_000_000, 512_000_000),
    ("b5 800", 806_000_000, 960_000_000),
)

PMR = tuple(range(446_006_250, 446_093_751, 12_500))

# One carrier per legacy international marine channel. Duplex entries use
# the coast-station frequency. This reconstruction uses Ofcom's Appendix 18
# table, since Uniden publishes only the preset count.
_MARINE_SIMPLEX = {6, *range(8, 18), *range(67, 78), 87, 88}
_MARINE_NUMBERS = (*range(1, 29), *range(60, 89))


def _marine_frequency(number):
    if number < 60:
        ship = 156_000_000 + 50_000 * number
    else:
        ship = 156_025_000 + 50_000 * (number - 60)
    return ship if number in _MARINE_SIMPLEX else ship + 4_600_000


def factory_banks(air_step):
    air = BANDS[AIR_INDEX]
    return {
        "PVT": (),
        "FAV": (),
        "PMR": tuple(Channel(hz, "FM", "PMR", preset_number=index)
                     for index, hz in enumerate(PMR, 1)),
        "AIR": tuple(Channel(air.frequency(index, air_step), "AM", "AIR")
                     for index in range(air.count(air_step))),
        "MRN": tuple(Channel(_marine_frequency(number), "FM", "MRN", preset_number=number)
                     for number in _MARINE_NUMBERS),
        "CBAM": tuple(Channel(hz, "AM", "CBAM") for hz in range(25_000_000, 28_000_000, 5_000)),
        "CBFM": tuple(Channel(hz, "FM", "CBFM") for hz in range(28_000_000, 30_000_000, 5_000)),
    }
