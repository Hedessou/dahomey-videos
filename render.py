#!/usr/bin/env python3
"""
Rendu d'un épisode « Histoire du Dahomey » en vidéo verticale 1080x1920.

Entrée : un fichier JSON (voir exemples/episode.json) avec au minimum
    title, script (3 blocs séparés par |||), image_query
Sortie (dans le dossier donné) :
    video.mp4, cover.jpg, description.txt, meta.json

Gratuit : voix Microsoft Edge (edge-tts), images Wikimedia Commons, ffmpeg.
"""
import asyncio
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

W, H, FPS = 1080, 1920, 30
VOICE = os.environ.get("VOICE", "fr-FR-DeniseNeural")
RATE = os.environ.get("VOICE_RATE", "-4%")
UA = "DahomeyHistoryBot/1.0 (https://github.com; video rendering of public-domain history images)"
FALLBACK_QUERIES = ["Royal Palaces of Abomey", "Kingdom of Dahomey", "Abomey bas-relief", "Ouidah"]
SERIES_LABEL = os.environ.get("SERIES_LABEL", "HISTOIRE DU DAHOMEY")
HASHTAGS = os.environ.get(
    "HASHTAGS", "#Bénin #Dahomey #HistoireAfricaine #Abomey #Histoire #Afrique"
)
FONT_DIR = Path(__file__).parent / "fonts"


def log(*a):
    print("[render]", *a, flush=True)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[-4000:])
        raise RuntimeError("ffmpeg a échoué : " + " ".join(cmd[:6]))
    return r.stdout


def probe_duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)])
    return float(out.strip())


# ---------------------------------------------------------------- texte
def split_blocks(script: str):
    """Toujours 3 blocs, même si l'IA n'a pas respecté le séparateur |||."""
    script = (script or "").strip()
    parts = [p.strip() for p in script.split("|||") if p.strip()]
    if len(parts) == 3:
        return parts
    text = " ".join(parts) if parts else script
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    sentences = [s for s in sentences if s]
    if len(sentences) < 3:
        words = text.split()
        n = max(1, math.ceil(len(words) / 3))
        out = [" ".join(words[i:i + n]) for i in range(0, len(words), n)][:3]
        return out + ["…"] * (3 - len(out))
    total = sum(len(s) for s in sentences)
    blocks, cur, acc = [], [], 0
    for s in sentences:
        cur.append(s)
        acc += len(s)
        if len(blocks) < 2 and acc >= total * (len(blocks) + 1) / 3:
            blocks.append(" ".join(cur))
            cur = []
    blocks.append(" ".join(cur))
    blocks = [b for b in blocks if b.strip()]
    while len(blocks) < 3:  # cas extrêmes
        longest = max(range(len(blocks)), key=lambda i: len(blocks[i]))
        w = blocks[longest].split()
        half = len(w) // 2
        blocks[longest:longest + 1] = [" ".join(w[:half]), " ".join(w[half:])]
    return blocks[:3]


# ---------------------------------------------------------------- voix
async def _edge_tts(text, mp3_path):
    import edge_tts
    try:
        com = edge_tts.Communicate(text, VOICE, rate=RATE, boundary="WordBoundary")
    except TypeError:  # anciennes versions
        com = edge_tts.Communicate(text, VOICE, rate=RATE)
    words = []
    with open(mp3_path, "wb") as f:
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                words.append([chunk["text"], start, start + chunk["duration"] / 1e7])
    return words


def estimate_words(text, duration):
    """Timings approximatifs (repli quand la voix ne donne pas les mots)."""
    toks = text.split()
    total = sum(len(t) + 1 for t in toks) or 1
    t, out = 0.15, []
    span = max(0.5, duration - 0.3)
    for tok in toks:
        d = span * (len(tok) + 1) / total
        out.append([tok, t, t + d])
        t += d
    return out


def tts(text, mp3_path):
    """Retourne la liste [mot, début, fin]. edge-tts, sinon gTTS, sinon voix test."""
    if os.environ.get("FAKE_TTS"):
        dur = max(3.0, len(text) / 15.0)
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=220:duration={dur}",
             "-q:a", "4", str(mp3_path)])
        return estimate_words(text, dur)
    last = None
    for attempt in range(3):
        try:
            words = asyncio.run(_edge_tts(text, mp3_path))
            if Path(mp3_path).stat().st_size > 1000:
                if not words:
                    words = estimate_words(text, probe_duration(mp3_path))
                return words
        except Exception as e:  # réseau, quota…
            last = e
            log("edge-tts échec", attempt + 1, e)
            time.sleep(3 * (attempt + 1))
    log("repli sur gTTS :", last)
    from gtts import gTTS
    gTTS(text, lang="fr").save(str(mp3_path))
    return estimate_words(text, probe_duration(mp3_path))


def align_punctuation(words, text):
    """Remet la ponctuation du texte original sur les mots d'edge-tts."""
    toks = text.split()
    if len(toks) == len(words):
        for w, t in zip(words, toks):
            w[0] = t
    return words


# ---------------------------------------------------------------- images
def wikimedia_images(query, n=3):
    try:
        r = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query", "format": "json", "generator": "search",
                "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6,
                "gsrlimit": 12, "prop": "imageinfo",
                "iiprop": "url|mime|size", "iiurlwidth": 1600,
            },
            headers={"User-Agent": UA}, timeout=20,
        )
        pages = (r.json().get("query") or {}).get("pages") or {}
    except Exception as e:
        log("wikimedia indisponible :", e)
        return []
    found = []
    for p in sorted(pages.values(), key=lambda p: p.get("index", 99)):
        info = (p.get("imageinfo") or [{}])[0]
        if info.get("mime") not in ("image/jpeg", "image/png"):
            continue
        if min(info.get("width", 0), info.get("height", 0)) < 400:
            continue
        found.append(info.get("thumburl") or info.get("url"))
    return found[:n]


def download(url, path):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    path.write_bytes(r.content)
    return path


def collect_images(queries, work, n=3):
    urls = []
    for q in queries:
        if not q:
            continue
        for u in wikimedia_images(q, n):
            if u not in urls:
                urls.append(u)
        if len(urls) >= n:
            break
    files = []
    for i, u in enumerate(urls[:n]):
        try:
            files.append(download(u, work / f"img{i}{Path(u.split('?')[0]).suffix or '.jpg'}"))
        except Exception as e:
            log("téléchargement raté", u, e)
    while len(files) < n:  # fond de secours : dégradé ocre/terre
        p = work / f"fallback{len(files)}.png"
        run(["ffmpeg", "-y", "-f", "lavfi", "-i",
             f"gradients=s={W}x{H}:c0=0x3b1f0e:c1=0x8a4b1c:x0=0:y0=0:x1={W}:y1={H}:d=1",
             "-frames:v", "1", str(p)])
        files.append(p)
    return files, urls[:n]


# ---------------------------------------------------------------- sous-titres
def ass_time(t):
    t = max(0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass_escape(s):
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def wrap(text, width):
    lines, cur = [], ""
    for w in text.split():
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return r"\N".join(lines)


def build_ass(path, words, title, label, teaser, total, title_font, body_font):
    """Sous-titres façon TikTok (3-5 mots), titre au début, teaser à la fin."""
    events = []
    # légende par groupes de mots
    chunk = []
    groups = []
    for w in words:
        chunk.append(w)
        if len(chunk) >= 5 or re.search(r"[.,;:!?…]$", w[0]):
            groups.append(chunk)
            chunk = []
    if chunk:
        groups.append(chunk)
    for i, g in enumerate(groups):
        start = g[0][1]
        end = groups[i + 1][0][1] if i + 1 < len(groups) else g[-1][2] + 0.3
        end = min(end, g[-1][2] + 0.6)
        txt = " ".join(x[0] for x in g)
        events.append(f"Dialogue: 1,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,"
                      + wrap(ass_escape(txt), 22))
    # bandeau série tout le long
    events.append(f"Dialogue: 0,{ass_time(0)},{ass_time(total)},Label,,0,0,0,,{ass_escape(label)}")
    # titre les 5 premières secondes
    events.append(f"Dialogue: 2,{ass_time(0.2)},{ass_time(min(5.5, total))},Title,,0,0,0,,"
                  r"{\fad(300,400)}" + wrap(ass_escape(title.upper()), 18))
    # teaser à la fin
    if teaser:
        events.append(f"Dialogue: 2,{ass_time(max(0, total - 6))},{ass_time(total)},Teaser,,0,0,0,,"
                      r"{\fad(400,0)}" + wrap(ass_escape("DEMAIN : " + teaser), 26))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{body_font},64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,5,2,2,70,70,560,1
Style: Title,{title_font},92,&H0000C8FF,&H00FFFFFF,&H00000000,&H96000000,1,0,0,0,100,100,1,0,1,6,3,8,60,60,260,1
Style: Label,{body_font},38,&H00E6F0FF,&H00FFFFFF,&H00000000,&H78000000,1,0,0,0,100,100,4,0,3,10,0,8,60,60,130,1
Style: Teaser,{title_font},66,&H00FFFFFF,&H00FFFFFF,&H00000000,&HB4103A6B,1,0,0,0,100,100,0,0,3,18,0,5,80,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def pick_fonts():
    """Anton pour les titres si dispo (téléchargée par le workflow), sinon DejaVu."""
    title = "Anton" if (FONT_DIR / "Anton-Regular.ttf").exists() else "DejaVu Sans"
    return title, "DejaVu Sans"


# ---------------------------------------------------------------- vidéo
MOVES = [
    # zoom avant centré
    ("min(1+0.0009*on,1.18)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    # zoom avant + glissement vers la droite
    ("min(1.08+0.0005*on,1.18)", "(iw-iw/zoom)*on/{F}", "ih/2-(ih/zoom/2)"),
    # dézoom
    ("max(1.18-0.0009*on,1.0)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
]


def make_still(img, out):
    """Image entière posée sur un fond flou de la même image (pas de tête coupée)."""
    fc = (
        f"[0:v]scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},"
        f"boxblur=40:2,eq=brightness=-0.18:saturation=0.8[bg];"
        f"[0:v]scale={W*2-80}:{int(H*2*0.62)}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2-120,vignette=PI/4,format=yuv420p"
    )
    run(["ffmpeg", "-y", "-i", str(img), "-filter_complex", fc,
         "-frames:v", "1", "-q:v", "2", str(out)])


def make_segment(still, audio, dur, idx, out):
    frames = int(math.ceil(dur * FPS))
    z, x, y = MOVES[idx % len(MOVES)]
    x = x.replace("{F}", str(frames))
    vf = (f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},"
          f"fade=t=in:st=0:d=0.35,fade=t=out:st={dur-0.35:.2f}:d=0.35,format=yuv420p")
    run(["ffmpeg", "-y", "-loop", "1", "-framerate", str(FPS), "-i", str(still),
         "-i", str(audio), "-filter_complex",
         f"[0:v]{vf}[v];[1:a]aresample=44100,apad,atrim=0:{dur:.3f}[a]",
         "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2", str(out)])


def main(payload_path, out_dir):
    ep = json.loads(Path(payload_path).read_text(encoding="utf-8"))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="render_"))

    title = (ep.get("title") or "Histoire du Dahomey").strip()
    blocks = split_blocks(ep.get("script", ""))
    episode = str(ep.get("episode") or ep.get("current_episode") or "").strip()
    label = f"{SERIES_LABEL} · ÉP. {episode}" if episode else SERIES_LABEL
    teaser = (ep.get("next_event") or "").strip()
    log("épisode", episode, "—", title)

    images, image_urls = collect_images(
        [ep.get("image_query"), ep.get("period"), *FALLBACK_QUERIES], work)

    all_words, segments, offset = [], [], 0.0
    for i, text in enumerate(blocks):
        mp3 = work / f"voice{i}.mp3"
        words = align_punctuation(tts(text, mp3), text)
        dur = probe_duration(mp3) + (0.6 if i < 2 else 1.2)
        if i == 2 and teaser:
            dur = max(dur, 6.5)
        still = work / f"still{i}.jpg"
        make_still(images[i], still)
        seg = work / f"seg{i}.mp4"
        make_segment(still, mp3, dur, i, seg)
        for w, s, e in words:
            all_words.append([w, s + offset, e + offset])
        segments.append(seg)
        offset += dur
        log(f"bloc {i+1}: {dur:.1f}s")

    concat = work / "list.txt"
    concat.write_text("".join(f"file '{s}'\n" for s in segments))
    joined = work / "joined.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(joined)])
    total = probe_duration(joined)

    ass = work / "subs.ass"
    tf, bf = pick_fonts()
    build_ass(ass, all_words, title, label, teaser, total, tf, bf)
    fontsdir = f":fontsdir={FONT_DIR}" if FONT_DIR.exists() else ""
    video = out / "video.mp4"
    run(["ffmpeg", "-y", "-i", str(joined), "-vf", f"ass={ass}{fontsdir}",
         "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
         "-profile:v", "high", "-r", str(FPS), "-c:a", "copy", "-movflags", "+faststart", str(video)])
    run(["ffmpeg", "-y", "-ss", "1.5", "-i", str(video), "-frames:v", "1", "-q:v", "3",
         str(out / "cover.jpg")])

    source = (ep.get("source") or "").strip()
    desc = f"{title}\n\n"
    if episode:
        desc += f"Épisode {episode} de notre série sur l'histoire du royaume du Dahomey.\n"
    if teaser:
        desc += f"Demain : {teaser}\n"
    if source:
        desc += f"\nSources : {source}\n"
    desc += f"\n{HASHTAGS}"
    (out / "description.txt").write_text(desc, encoding="utf-8")

    meta = {k: ep.get(k) for k in ("episode", "title", "next_episode", "next_period",
                                   "next_event", "source", "source_url")}
    meta.update(duration=round(total, 1), images=image_urls, description=desc)
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"OK {video} ({total:.1f}s)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "out")
