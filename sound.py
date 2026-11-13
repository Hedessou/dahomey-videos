"""
Musique et bruitages générés par le programme (aucun fichier externe, aucun droit d'auteur).
- percussions façon tambours d'Afrique de l'Ouest (grave + aigu + shaker) et nappe douce
- « whoosh » à chaque apparition de case et à chaque tourne de page
- « impact » sous chaque onomatopée et sous le titre
"""
import wave

import numpy as np

SR = 44100


def _env(n, attack, decay):
    t = np.arange(n) / SR
    a = np.clip(t / max(attack, 1e-4), 0, 1)
    return a * np.exp(-t / max(decay, 1e-4))


def _lowpass(x, alpha):
    """Filtre passe-bas d'ordre 1 ; alpha peut varier dans le temps (tableau)."""
    y = np.empty_like(x)
    acc = 0.0
    al = np.broadcast_to(alpha, x.shape)
    for i in range(len(x)):
        acc += al[i] * (x[i] - acc)
        y[i] = acc
    return y


def drum_low(rng):
    n = int(0.45 * SR)
    t = np.arange(n) / SR
    f = 95 * np.exp(-t * 9) + 52
    ph = 2 * np.pi * np.cumsum(f) / SR
    return np.sin(ph) * _env(n, 0.002, 0.16) * 0.9


def drum_high(rng):
    n = int(0.18 * SR)
    t = np.arange(n) / SR
    tone = np.sin(2 * np.pi * 330 * t) * 0.5 + np.sin(2 * np.pi * 510 * t) * 0.25
    noise = rng.normal(0, 1, n) * 0.35
    return (tone + noise) * _env(n, 0.001, 0.045) * 0.55


def shaker(rng):
    n = int(0.06 * SR)
    x = rng.normal(0, 1, n)
    x = x - _lowpass(x, 0.35)          # passe-haut
    return x * _env(n, 0.004, 0.018) * 0.22


def whoosh(rng, dur=0.42):
    n = int(dur * SR)
    x = rng.normal(0, 1, n)
    t = np.linspace(0, 1, n)
    alpha = 0.02 + 0.25 * np.sin(np.pi * t) ** 2
    y = _lowpass(x, alpha)
    return y * np.sin(np.pi * t) ** 1.5 * 1.6


def impact(rng):
    n = int(0.9 * SR)
    t = np.arange(n) / SR
    f = 70 * np.exp(-t * 4) + 38
    boom = np.sin(2 * np.pi * np.cumsum(f) / SR) * _env(n, 0.002, 0.30)
    crack = _lowpass(rng.normal(0, 1, n), 0.4) * _env(n, 0.001, 0.05) * 1.2
    return (boom + crack) * 0.9


def music(total, bpm=96, seed=5):
    rng = np.random.default_rng(seed)
    n = int(total * SR) + SR
    out = np.zeros(n)
    beat = 60 / bpm
    low, high, sh = drum_low(rng), drum_high(rng), shaker(rng)
    # motif sur 2 mesures (16 croches) : 1 = grave, 2 = aigu
    pattern = [1, 0, 0, 2, 0, 2, 1, 0, 1, 0, 2, 0, 0, 2, 2, 0]
    step = beat / 2
    k = 0
    t = 0.0
    while t < total:
        i = int(t * SR)
        p = pattern[k % 16]
        if p == 1:
            out[i:i + len(low)] += low[: max(0, min(len(low), n - i))]
        elif p == 2:
            v = 0.8 + 0.4 * rng.random()
            out[i:i + len(high)] += v * high[: max(0, min(len(high), n - i))]
        out[i:i + len(sh)] += (0.6 if k % 2 else 1.0) * sh[: max(0, min(len(sh), n - i))]
        k += 1
        t += step
    # nappe douce (quinte la / mi) avec léger trémolo
    tt = np.arange(n) / SR
    pad = (np.sin(2 * np.pi * 110 * tt) + 0.6 * np.sin(2 * np.pi * 164.8 * tt)
           + 0.3 * np.sin(2 * np.pi * 220 * tt)) * (0.55 + 0.45 * np.sin(2 * np.pi * 0.12 * tt))
    out += pad * 0.05
    # fondu entrée / sortie
    fade = np.ones(n)
    fi, fo = int(1.5 * SR), int(2.5 * SR)
    fade[:fi] = np.linspace(0, 1, fi)
    end = int(total * SR)
    fade[max(0, end - fo):end] = np.linspace(1, 0, min(fo, end))
    fade[end:] = 0
    return out * fade


def sfx_track(total, whooshes, impacts, seed=9):
    rng = np.random.default_rng(seed)
    n = int(total * SR) + SR
    out = np.zeros(n)
    w = whoosh(rng)
    for t in whooshes:
        i = int(max(0, t - 0.15) * SR)
        m = max(0, min(len(w), n - i))
        out[i:i + m] += w[:m] * 0.5
    im = impact(rng)
    for t in impacts:
        i = int(t * SR)
        m = max(0, min(len(im), n - i))
        out[i:i + m] += im[:m] * 0.8
    return out


def write_wav(path, x, gain=1.0):
    x = np.asarray(x) * gain
    peak = np.max(np.abs(x)) or 1.0
    if peak > 0.98:
        x = x / peak * 0.98
    data = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes(data.tobytes())
