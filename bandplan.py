"""Receiver grids and factory bank data for the photographed scanner."""

from dataclasses import dataclass, replace
from typing import NamedTuple

from ofcom_presets import BUSINESS_FM, COASTAL_FM, EVENTS_FM, RAIL_FM, UTILITIES_FM


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
    Band("25:30", 25_000_000, 29_999_999, 5_000),
    Band("30:50", 30_000_000, 49_999_999, 5_000),
    Band("50:80", 50_000_000, 79_999_999, 5_000),
    Band("80:83", 80_000_000, 82_999_999, 5_000),
    Band("83:84", 83_000_000, 84_014_999, 5_000),
    Band("84:87", 84_015_000, 87_299_999, 5_000),
    Band("108:137", 108_000_000, 136_999_999, AIR_STEP, "AM"),
    Band("137:138", 137_000_000, 137_999_999),
    Band("138:144", 138_000_000, 143_999_999),
    Band("144:146", 144_000_000, 145_999_999),
    Band("146:156", 146_000_000, 155_999_999),
    Band("156:157", 156_000_000, 157_437_499),
    Band("157:158", 157_437_500, 157_999_999),
    Band("158:160", 158_000_000, 160_599_999),
    Band("160:162", 160_600_000, 162_031_249),
    Band("162:162", 162_031_250, 162_599_999),
    Band("162:174", 162_600_000, 173_999_999),
    Band("406:440", 406_000_000, 439_999_999),
    Band("440:450", 440_000_000, 449_999_999),
    Band("450:466", 450_000_000, 465_999_999),
    Band("466:470", 466_000_000, 469_999_999),
    Band("470:512", 470_000_000, 511_999_999),
    Band("806:960", 806_000_000, 959_999_999, 12_500),
    # Append ranges so existing saved band indexes keep their meaning.
    Band("0.1:25", 100_000, 24_999_999, 5_000, "AM"),
    Band("87.3:87.5", 87_300_000, 87_499_999, 5_000),
    Band("87.5:108", 87_500_000, 107_999_999, 100_000, "WFM"),
    Band("174:225", 174_000_000, 224_999_999, 12_500),
    Band("225:400", 225_000_000, 399_999_999, 12_500, "AM"),
    Band("400:406", 400_000_000, 405_999_999, 12_500),
    Band("512:806", 512_000_000, 805_999_999, 12_500),
    Band("960:1750", 960_000_000, 1_750_000_000, 12_500),
)
BAND_ORDER = tuple(sorted(range(len(BANDS)), key=lambda index: BANDS[index].lower))

GERMAN_CHANGES = {
    0: {"modulation": "AM"},
    5: {"step": 20_000},
    7: {"step": 5_000},
    8: {"step": 5_000},
    9: {"step": 12_500},
    10: {"step": 10_000},
    11: {"step": 12_500},
    12: {"step": 12_500},
    13: {"step": 12_500},
    14: {"upper": 162_029_999, "step": 12_500},
    15: {"lower": 162_030_000, "step": 10_000},
    16: {"step": 10_000},
    19: {"step": 10_000},
    20: {"step": 10_000},
}
GERMAN_BANDS = tuple(replace(band, **GERMAN_CHANGES.get(index, {}))
                     for index, band in enumerate(BANDS))

BANKS = ("PVT", "FAV", "PMR", "AIR", "MRN", "CBAM", "CBFM", "BUS", "HAM", "BCAST", "RAIL", "EVT", "UTIL")
BANK_KEYS = {
    "private": ("PVT",),
    "fav": ("FAV",),
    "pmr": ("PMR",),
    "air": ("AIR", "MRN"),
    "cb": ("CBAM", "CBFM"),
    "business": ("BUS",),
    "amateur": ("HAM",),
    "fmradio": ("BCAST",),
    "rail": ("RAIL",),
    "events": ("EVT",),
    "utilities": ("UTIL",),
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

PMR = tuple(range(446_006_250, 446_193_751, 12_500))

# RSGB UK band plan and ETCC analogue repeaters and simplex gateways.
# Start with the calling channels and selected Anglia repeater outputs.
AMATEUR_FM = tuple(dict.fromkeys((
    145_500_000, 433_500_000, 433_050_000, 145_625_000, 433_000_000,
    430_812_500, 439_425_000, 1_297_000_000, 1_297_350_000,
    # 2 m simplex, repeater outputs, and FM downlink for ISS voice.
    144_962_500,
    *range(145_200_000, 145_562_501, 12_500),
    *range(145_575_000, 145_787_501, 12_500),
    145_800_000,
    # 70 cm FM voice gateways, repeater outputs, and simplex.
    *range(430_012_500, 430_100_001, 12_500),
    *range(430_812_500, 430_975_001, 12_500),
    *range(433_000_000, 433_375_001, 12_500),
    *range(433_400_000, 433_575_001, 12_500),
    438_387_500, 439_112_500, 439_325_000, 439_375_000, 439_725_000,
    # 10 m calling, simplex, voice gateways, and repeater outputs.
    29_600_000,
    *range(29_100_000, 29_200_000, 10_000),
    29_270_000, 29_280_000, 29_290_000,
    *range(29_610_000, 29_690_001, 10_000),
    # 6 m calling, voice gateways, repeater outputs, and simplex.
    51_510_000, 50_520_000, 50_530_000, 50_540_000,
    *range(50_720_000, 50_870_001, 10_000),
    *range(51_430_000, 51_590_001, 20_000),
    # 4 m calling, voice gateways, and simplex.
    70_450_000, 70_300_000, 70_350_000, 70_362_500, 70_387_500,
    70_412_500, 70_425_000, 70_475_000,
    # 23 cm repeater outputs, simplex, and FM voice gateways.
    *range(1_297_000_000, 1_297_375_001, 25_000),
    *range(1_297_500_000, 1_297_725_000, 25_000),
    1_297_750_000,
    *range(1_297_900_000, 1_297_975_001, 25_000),
    # Additional ETCC repeater outputs and gateways with an analogue mode.
    70_260_000, 70_437_500, 144_562_500, 144_912_500, 430_250_000,
    430_287_500, 430_300_000, 430_987_500, 432_800_000, 438_487_500,
    439_150_000, 439_337_500, 439_350_000, 439_362_500, 439_450_000,
    439_462_500, 439_487_500, 439_500_000, 439_512_500, 439_600_000,
    439_625_000, 439_675_000, 439_687_500, 439_750_000, 439_775_000,
    # Additional community, event and talk-through voice channels.
    51_650_000, 51_700_000, 51_750_000, 51_970_000, 51_990_000,
    70_375_000, 70_400_000, 144_625_000, 144_650_000, 144_675_000,
    144_750_000, 144_775_000, 430_800_000, 432_775_000, 433_700_000,
    433_725_000, 433_750_000, 433_775_000, 434_375_000, 438_400_000,
    # FM repeater inputs can also carry directly receivable local stations.
    *range(29_520_000, 29_590_001, 10_000),
    *range(51_220_000, 51_370_001, 10_000),
    51_720_000, 51_730_000,
    *range(51_770_000, 51_870_001, 10_000),
    144_537_500,
    *range(144_975_000, 145_187_501, 12_500),
    430_112_500, 430_150_000, 430_325_000, 430_337_500, 430_350_000,
    430_362_500, 430_375_000, 430_425_000, 430_450_000, 430_462_500,
    430_487_500, 430_500_000, 430_512_500, 430_600_000, 430_625_000,
    430_675_000, 430_687_500, 430_725_000, 430_775_000, 430_787_500,
    *range(434_600_000, 434_975_001, 12_500),
    *range(438_000_000, 438_375_001, 12_500),
    *range(438_412_500, 438_587_501, 12_500),
    439_250_000, 439_287_500,
    # ARISS and AMSAT nominal FM voice downlinks.
    145_900_000, 145_960_000, 435_400_000, 435_880_000, 436_400_000,
    436_795_000, 436_950_000, 437_200_000, 437_800_000,
    # Further channelised 23 cm FM/DV and mixed analogue/digital segments.
    *range(1_240_775_000, 1_240_975_001, 25_000),
    *range(1_241_775_000, 1_241_975_001, 25_000),
    *range(1_249_025_000, 1_249_225_001, 25_000),
    1_257_350_000, 1_277_350_000,
    *range(1_291_000_000, 1_291_375_001, 25_000),
    *range(1_298_025_000, 1_298_975_001, 25_000),
    *range(1_299_775_000, 1_299_975_001, 25_000),
)))

# RSGB AM activity and VMARS frequencies. Sample the unchannelised
# 10 m AM segment in 10 kHz steps.
AMATEUR_AM = (
    70_260_000, 144_550_000, 3_615_000, 29_000_000,
    5_317_000, 7_143_000, 14_286_000, 21_425_000,
    *range(29_010_000, 29_100_000, 10_000),
)

# Ofcom TxParams entries for West Runton, Tacolneston, Stody, Fakenham,
# Aylmerton, Great Massingham and Stoke Holy Cross lead the UK FM band grid.
BROADCAST_FM = tuple(dict.fromkeys((
    95_600_000, 89_700_000, 91_900_000, 94_100_000, 95_100_000,
    96_200_000, 99_300_000, 101_500_000, 103_200_000, 104_400_000, 106_100_000,
    *range(87_500_000, 108_000_000, 100_000),
)))

# Ofcom IR 2027 and IR 2027.2 channel centres, in channel-number order.
CB_CEPT = (
    26_965_000, 26_975_000, 26_985_000, 27_005_000, 27_015_000,
    27_025_000, 27_035_000, 27_055_000, 27_065_000, 27_075_000,
    27_085_000, 27_105_000, 27_115_000, 27_125_000, 27_135_000,
    27_155_000, 27_165_000, 27_175_000, 27_185_000, 27_205_000,
    27_215_000, 27_225_000, 27_255_000, 27_235_000, 27_245_000,
    27_265_000, 27_275_000, 27_285_000, 27_295_000, 27_305_000,
    27_315_000, 27_325_000, 27_335_000, 27_345_000, 27_355_000,
    27_365_000, 27_375_000, 27_385_000, 27_395_000, 27_405_000,
)
CB_UK = tuple(range(27_601_250, 27_991_251, 10_000))

# Appendix 18 voice channels from Ofcom's Ship Radio Licences guidance.
# List coast first on duplex channels, then add the ship side.
_MARINE_SIMPLEX = {6, *range(8, 18), *range(67, 78), 87, 88}
_MARINE_NUMBERS = (*range(1, 29), *range(60, 70), *range(71, 89))


def _marine_frequencies(number):
    if number < 60:
        ship = 156_000_000 + 50_000 * number
    else:
        ship = 156_025_000 + 50_000 * (number - 60)
    return (ship,) if number in _MARINE_SIMPLEX else (ship + 4_600_000, ship)


def _marine_channels():
    international = tuple(Channel(hz, "FM", "MRN", preset_number=number)
                          for number in _MARINE_NUMBERS for hz in _marine_frequencies(number))
    international_hz = {channel.hz for channel in international}
    return international + tuple(Channel(hz, "FM", "MRN")
                                 for hz in COASTAL_FM if hz not in international_hz)


def factory_banks(air_step):
    air = BANDS[AIR_INDEX]
    return {
        "PVT": (),
        "FAV": (),
        "PMR": tuple(Channel(hz, "FM", "PMR", preset_number=index)
                     for index, hz in enumerate(PMR, 1)),
        "AIR": tuple(Channel(air.frequency(index, air_step), "AM", "AIR")
                     for index in range(air.count(air_step))),
        "MRN": _marine_channels(),
        "CBAM": tuple(Channel(hz, "AM", "CBAM", preset_number=number)
                      for number, hz in enumerate(CB_CEPT, 1)),
        "CBFM": tuple(Channel(hz, "FM", "CBFM", preset_number=number)
                      for frequencies in (CB_UK, CB_CEPT) for number, hz in enumerate(frequencies, 1)),
        "BUS": tuple(Channel(hz, "FM", "BUS") for hz in BUSINESS_FM),
        "HAM": tuple(Channel(hz, "FM", "HAM") for hz in AMATEUR_FM)
               + tuple(Channel(hz, "AM", "HAM") for hz in AMATEUR_AM),
        "BCAST": tuple(Channel(hz, "WFM", "BCAST") for hz in BROADCAST_FM),
        "RAIL": tuple(Channel(hz, "FM", "RAIL") for hz in RAIL_FM),
        "EVT": tuple(Channel(hz, "FM", "EVT") for hz in EVENTS_FM),
        "UTIL": tuple(Channel(hz, "FM", "UTIL") for hz in UTILITIES_FM),
    }
