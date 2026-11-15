"""Streaming ITA2 (Baudot) receiver for mark/space radio teletype."""

from collections import deque

import numpy as np
from scipy import signal

LETTERS = "\0E\nA SIU\rDRJNFCKTZLWHYPQOBG\0MXV\0"
# The figures table differs by national variant. These are the ITA2
# assignments used for common amateur and weather RTTY reception.
FIGURES = (
    "\0", "3", "\n", "-", " ", "'", "8", "7", "\r", "$", "4", "\a", ",", "!", ":", "(",
    "5", "+", ")", "2", "#", "6", "0", "1", "9", "?", "&", "", ".", "/", ";", "",
)
FIGURES_SHIFT, LETTERS_SHIFT = 27, 31
DETECT_RATE = 600


class RttyDecoder:
    def __init__(self, sample_rate, *, baud=45.45, reverse=False, usos=True, shift=170, newline="any"):
        self.period = DETECT_RATE / baud
        self.reverse = reverse
        self.usos = usos
        self.threshold = shift * .11
        self.newline = newline
        self.sections = signal.butter(2, min(baud * .85, shift * .35), fs=sample_rate, output="sos")
        self.filter_state = np.zeros((len(self.sections), 2))
        self.decimation = sample_rate // DETECT_RATE
        self.offset = 0
        self.previous_iq = 0j
        self.sample_rate = sample_rate
        self.previous_mark = True
        self.at = None
        self.index = 0
        self.bits = []
        self.figures = False
        self.blank = False
        self.text = deque(maxlen=2400)

    def feed(self, iq):
        if not len(iq):
            return
        previous = np.empty_like(iq)
        previous[0] = self.previous_iq
        previous[1:] = iq[:-1]
        self.previous_iq = iq[-1]
        frequency = np.angle(iq * previous.conj()) * (self.sample_rate / (2 * np.pi))
        smooth, self.filter_state = signal.sosfilt(self.sections, frequency, zi=self.filter_state)
        for sample in smooth[self.offset::self.decimation]:
            if self.reverse:
                sample = -sample
            mark = self.previous_mark if abs(sample) < self.threshold else bool(sample > 0)
            self._bit(mark)
        self.offset = (self.offset - len(iq)) % self.decimation

    def _bit(self, mark):
        if self.at is None:
            if not mark and self.previous_mark:
                self.at = self.index + self.period * .5
                self.bits = []
        elif self.index >= self.at:
            self._sample(mark)
        self.previous_mark = mark
        self.index += 1

    def _sample(self, mark):
        if not self.bits and mark:
            self.at = None
            return
        if len(self.bits) >= 6:
            if mark:
                code = sum(int(bit) << place for place, bit in enumerate(self.bits[1:]))
                self._character(code)
            self.at = None
        else:
            self.bits.append(mark)
            self.at += self.period

    def _character(self, code):
        if code == LETTERS_SHIFT or code == FIGURES_SHIFT:
            self.figures = code == FIGURES_SHIFT
            return
        char = (FIGURES if self.figures else LETTERS)[code]
        if char == "\r":
            self.blank = True
            if self.newline == "pair":
                return
            char = "\n"
        elif char == "\n":
            if self.newline == "pair" and not self.blank:
                return
            if self.newline != "pair" and self.blank:
                self.blank = False
                return
            self.blank = False
        else:
            self.blank = False
        if char and (char.isprintable() or char == "\n"):
            self.text.append(char)
        if self.usos and char == " ":
            self.figures = False

    def snapshot(self):
        return "".join(self.text)
