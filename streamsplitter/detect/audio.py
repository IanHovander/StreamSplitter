"""Field-sound detector: finds the FMS cues (start sound, buzzer, bells, whistle) in stream audio.

Each cue is a template: a patch of whitened log spectrogram that starts a little before the cue's onset. A cue is
found where the normalized cross-correlation (NCC) of the stream's spectrogram with the template peaks above a
threshold. The cue time is the peak time plus the template's lead-in, i.e. the onset.

Chunks arrive one at a time (`AudioDetector.feed`). The last few seconds of audio are carried into the next chunk,
so a cue that straddles a chunk boundary is still found, and each peak is reported once.

Times are stream seconds: the chunk's `t0` (its first video frame) plus the audio's offset inside the chunk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import media

SR = 16000
N_FFT = 1024
HOP = 160                     # 10 ms frames
FPS = SR / HOP
F_LO, F_HI = 150.0, 5000.0    # the cues' harmonics; below is rumble and crowd, above adds little
_FREQS = np.fft.rfftfreq(N_FFT, 1 / SR)
_BAND = (_FREQS >= F_LO) & (_FREQS <= F_HI)
_WIN = np.hanning(N_FFT).astype(np.float32)


SMOOTH = 31                   # frames (0.31 s) of moving average along time; see logspec


def logspec(x: np.ndarray, smooth: int = SMOOTH) -> np.ndarray:
    """Whitened log-magnitude spectrogram, shape (bins, frames), bins pooled in pairs.

    Smoothing: a moving average along time over `smooth` frames. The FMS cues are steady tones; commentators' voices
    glide in pitch, so averaging over 0.3 s keeps the cues and smears the speech (on 2026cancmp qm40 the true-cue NCC
    rose from 0.39-0.48 to 0.61-0.73 while the best non-cue value rose only from 0.23 to 0.36).
    Whitening: each frame minus its mean over frequency (loudness changes), then each bin minus its median over
    time (the venue's and the mix's colouring). What remains is the shape of the sound, which is what the
    templates compare."""
    if len(x) < N_FFT:
        return np.zeros((int(_BAND.sum()) // 2, 0), np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(x, N_FFT)[::HOP] * _WIN
    mag = np.abs(np.fft.rfft(frames, axis=1))[:, _BAND]
    mag = mag[:, : mag.shape[1] // 2 * 2].reshape(len(mag), -1, 2).mean(axis=2)
    s = np.log(mag + 1e-4).astype(np.float32).T
    if smooth > 1:
        c = np.cumsum(np.pad(s, ((0, 0), (smooth // 2 + 1, smooth // 2)), mode="edge"), axis=1, dtype=np.float64)
        s = ((c[:, smooth:] - c[:, :-smooth]) / smooth).astype(np.float32)
    s -= s.mean(axis=0, keepdims=True)
    s -= np.median(s, axis=1, keepdims=True)
    return s


def ncc(spec: np.ndarray, patch: np.ndarray) -> np.ndarray:
    """NCC of `patch` (bins, L) slid along `spec` (bins, T); returns T - L + 1 values in [-1, 1]."""
    nb, L = patch.shape
    T = spec.shape[1]
    if T < L:
        return np.zeros(0, np.float32)
    p = patch - patch.mean()
    p_norm = np.sqrt((p * p).sum())
    n = 1 << int(np.ceil(np.log2(T + L)))
    num = np.fft.irfft((np.fft.rfft(spec, n, axis=1) * np.conj(np.fft.rfft(p, n, axis=1))).sum(axis=0), n)[: T - L + 1]
    cs = np.concatenate([[0.0], np.cumsum(spec.sum(axis=0), dtype=np.float64)])
    cs2 = np.concatenate([[0.0], np.cumsum((spec.astype(np.float64) ** 2).sum(axis=0))])
    s1 = cs[L:] - cs[:-L]
    s2 = cs2[L:] - cs2[:-L]
    var = np.maximum(s2 - s1 * s1 / (nb * L), 1e-9)
    return (num / (np.sqrt(var) * p_norm)).astype(np.float32)


@dataclass
class Template:
    name: str              # "start", "buzzer", "bells", "whistle"
    patch: np.ndarray      # (bins, L) whitened log spectrogram
    lead_s: float          # seconds of the patch before the cue's onset

    @classmethod
    def from_audio(cls, name: str, x: np.ndarray, onset_s: float, lead_s: float = 0.2, length_s: float = 1.5):
        """Cut a template from mono SR-Hz audio `x` whose cue starts at `onset_s` (spectrogram whitened in context)."""
        a = max(0, int((onset_s - lead_s - 5.0) * SR))
        spec = logspec(x[a: int((onset_s + length_s + 5.0) * SR)])
        c0 = int(round((onset_s - lead_s) * FPS)) - int(round(a / HOP))
        return cls(name, spec[:, c0: c0 + int(round(length_s * FPS))].copy(), lead_s)


def save_templates(path: Path, templates: list[Template]) -> None:
    np.savez_compressed(path, **{f"{t.name}__patch": t.patch for t in templates},
                        **{f"{t.name}__lead": np.float32(t.lead_s) for t in templates})


def load_templates(path: Path) -> list[Template]:
    z = np.load(path)
    names = sorted({k.split("__")[0] for k in z.files})
    return [Template(n, z[f"{n}__patch"], float(z[f"{n}__lead"])) for n in names]


def find_peaks(score: np.ndarray, thr: float, min_gap: int) -> list[int]:
    """Indices of local maxima >= thr, greedily keeping the highest within `min_gap` frames."""
    idx = np.flatnonzero(score >= thr)
    picked: list[int] = []
    for i in idx[np.argsort(-score[idx])]:
        if all(abs(i - j) >= min_gap for j in picked):
            picked.append(int(i))
    return sorted(picked)


class AudioDetector:
    """Feed chunks in order; get cue events {"kind": "audio", "cue", "t", "score"}."""

    def __init__(self, templates: list[Template], thr: float = 0.40, carry_s: float = 4.0):
        self.templates = templates
        self.thr = thr
        self.carry = int(carry_s * SR)
        self.buf = np.zeros(0, np.float32)
        self.buf_t0: float | None = None     # stream time of buf[0]
        self.done_t: dict[str, float] = {}   # per cue: time of the last one reported

    def feed(self, path, t0: float) -> list[dict]:
        starts = media.stream_starts(path)
        x = media.decode_audio(path, SR)
        if not len(x):
            return []
        a_t0 = t0 + (starts.get("audio", 0.0) - starts.get("video", 0.0))
        if self.buf_t0 is None or abs(self.buf_t0 + len(self.buf) / SR - a_t0) > 1.0:
            buf, buf_t0 = x, a_t0                         # first chunk, or a gap: start afresh
        else:
            gap = int(round((a_t0 - (self.buf_t0 + len(self.buf) / SR)) * SR))
            if gap >= 0:
                buf = np.concatenate([self.buf, np.zeros(gap, np.float32), x])
            else:
                buf = np.concatenate([self.buf[: len(self.buf) + gap], x])
            buf_t0 = self.buf_t0
        spec = logspec(buf)
        events = []
        for tpl in self.templates:
            sc = ncc(spec, tpl.patch)
            last = self.done_t.get(tpl.name, -np.inf)
            # 4 s apart: the buzzer sounds for ~3 s and its template also matches inside the sustain.
            for i in find_peaks(sc, self.thr, int(4.0 * FPS)):
                t = buf_t0 + i / FPS + tpl.lead_s
                # A peak whose window reaches the last 0.5 s may still grow with the next chunk's audio, which arrives
                # with the carried tail: leave it for then. A peak re-found in the carried tail is reported once.
                if i + tpl.patch.shape[1] > spec.shape[1] - int(0.5 * FPS) or t <= last + 1.0:
                    continue
                events.append({"kind": "audio", "cue": tpl.name, "t": round(t, 3), "score": round(float(sc[i]), 3)})
                last = t
            self.done_t[tpl.name] = last
        keep_from = max(0, len(buf) - self.carry)
        self.buf, self.buf_t0 = buf[keep_from:], buf_t0 + keep_from / SR
        return sorted(events, key=lambda e: e["t"])
