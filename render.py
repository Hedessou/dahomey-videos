#!/usr/bin/env python3
"""
Rendu d'un épisode « Histoire du Dahomey » : vidéo verticale 1080x1920 d'environ 1 min 30.

Structure :
  scène 1  : photo des Palais royaux d'Abomey (Wikimedia Commons)
  scènes 2+: planches de bande dessinée générées pour illustrer ce que dit la narration
Chaque scène = une case de BD encadrée + un « récitatif » (encadré jaune) synchronisé à la voix.

Entrée : JSON (voir exemples/episode.json) avec title + scenes[{narration, image_prompt}]
         (ancien format accepté : script "bloc|||bloc|||bloc").
Sortie : video.mp4, cover.jpg, description.txt, meta.json

Gratuit : voix Microsoft Edge (edge-tts), images Wikimedia + Pollinations, ffmpeg.
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
import urllib.parse
from pathlib import Path

import requests

W, H, FPS = 1080, 1920, 30
VOICE = os.environ.get("VOICE", "fr-FR-DeniseNeural")
RATE = os.environ.get("VOICE_RATE", "-4%")
UA = "DahomeyHistoryBot/1.0 (educational history videos; contact via GitHub)"
PALACE_QUERIES = ["Royal Palaces of Abomey", "Abomey palace", "Palais royaux d'Abomey"]
FALLBACK_QUERIES = ["Abomey bas-relief", "Kingdom of Dahomey", "Ouidah"]
SERIES_LABEL = os.environ.get("SERIES_LABEL", "HISTOIRE DU DAHOMEY")
HASHTAGS = os.environ.get(
    "HASHTAGS", "#Bénin #Dahomey #HistoireAfricaine #Abomey #Histoire #Afrique #BD"
)
COMIC_STYLE = os.environ.get(
    "COMIC_STYLE",
    "franco-belgian comic book panel, ligne claire style, bold clean ink outlines, "
    "flat vivid colors, detailed background, cinematic framing, "
    "Kingdom of Dahomey, West Africa, 17th-19th century, Black African Fon people, "
    "red laterite earth, mud-brick palaces with thatched roofs, palm trees, savanna, "
    "traditional West African cloth, cowries, parasols, no European buildings or clothing, "
    "dignified and respectful, no text, no letters, no speech bubbles, no watermark",
)
POLLI_TOKEN = os.environ.get("POLLINATIONS_TOKEN", "")
CF_ACCOUNT = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()
FONT_DIR = Path(__file__).parent / "fonts"

# mise en page
PANEL_W, PANEL_H = 960, 1180          # case de BD (≈ 4:5)
PANEL_X, PANEL_Y = (W - PANEL_W) // 2, 230
BORDER = 10
PAPER = "0xF1E4C3"                    # papier crème


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
def split_text(text, n):
    """Découpe un texte en n morceaux à peu près égaux, sur des fins de phrase."""
    sentences = [s for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s]
    if len(sentences) < n:
        words = text.split()
        k = max(1, math.ceil(len(words) / n))
        parts = [" ".join(words[i:i + k]) for i in range(0, len(words), k)]
        return (parts + ["…"] * n)[:n]
    total = sum(len(s) for s in sentences)
    parts, cur, acc = [], [], 0
    for s in sentences:
        cur.append(s)
        acc += len(s)
        if len(parts) < n - 1 and acc >= total * (len(parts) + 1) / n:
            parts.append(" ".join(cur))
            cur = []
    parts.append(" ".join(cur))
    parts = [p for p in parts if p.strip()]
    return (parts + ["…"] * n)[:n]


def get_scenes(ep):
    """Retourne [(narration, image_prompt)]. Accepte le nouveau et l'ancien format."""
    scenes = ep.get("scenes")
    if isinstance(scenes, str):
        try:
            scenes = json.loads(scenes)
        except json.JSONDecodeError:
            scenes = None
    out = []
    if isinstance(scenes, list):
        for s in scenes:
            if isinstance(s, dict):
                txt = (s.get("narration") or s.get("text") or "").strip()
                if txt:
                    out.append((txt, (s.get("image_prompt") or s.get("image") or "").strip()))
    if len(out) >= 2:
        return out
    script = (ep.get("script") or "").replace("|||", " ").strip()
    if not script:
        raise SystemExit("Aucune narration dans l'épisode.")
    return [(t, "") for t in split_text(script, 6)]


# ---------------------------------------------------------------- voix
async def _edge_tts(text, mp3_path):
    import edge_tts
    try:
        com = edge_tts.Communicate(text, VOICE, rate=RATE, boundary="WordBoundary")
    except TypeError:
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
                return words or estimate_words(text, probe_duration(mp3_path))
        except Exception as e:
            last = e
            log("edge-tts échec", attempt + 1, e)
            time.sleep(3 * (attempt + 1))
    log("repli sur gTTS :", last)
    from gtts import gTTS
    gTTS(text, lang="fr").save(str(mp3_path))
    return estimate_words(text, probe_duration(mp3_path))


def align_punctuation(words, text):
    """Affiche les mots du texte original (avec ponctuation) sur les timings de la voix."""
    toks = text.split()
    if not words or not toks:
        return words
    if len(toks) == len(words):
        for w, t in zip(words, toks):
            w[0] = t
        return words
    nb, nt = len(words), len(toks)
    out = []
    for i, t in enumerate(toks):
        j = min(nb - 1, (i * nb) // nt)
        out.append([t, words[j][1], words[j][2]])
    for i in range(len(out) - 1):
        out[i][2] = max(out[i][1] + 0.05, out[i + 1][1])
    out[-1][2] = max(out[-1][2], words[-1][2])
    return out


# ---------------------------------------------------------------- images
def wikimedia_images(query, n=3):
    if os.environ.get("OFFLINE"):
        return []
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
        if min(info.get("width", 0), info.get("height", 0)) < 500:
            continue
        found.append(info.get("thumburl") or info.get("url"))
    return found[:n]


def download(url, path, timeout=60, headers=None):
    h = {"User-Agent": UA}
    h.update(headers or {})
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    if not r.headers.get("content-type", "").startswith("image") or len(r.content) < 5000:
        raise ValueError(f"pas une image ({r.headers.get('content-type')}, {len(r.content)} o)")
    path.write_bytes(r.content)
    return path


def wikimedia_file(queries, path, skip=()):
    for q in queries:
        for u in wikimedia_images(q, 6):
            if u in skip:
                continue
            try:
                return download(u, path), u
            except Exception as e:
                log("wikimedia raté", e)
    return None, None


_last_polli = [0.0]


def cloudflare_image(full, path, seed):
    """FLUX-1-schnell sur Cloudflare Workers AI (gratuit : 10 000 neurons/jour, sans filigrane)."""
    import base64
    url = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}"
           "/ai/run/@cf/black-forest-labs/flux-1-schnell")
    for attempt in range(3):
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {CF_TOKEN}"},
                              json={"prompt": full[:2000], "steps": 6}, timeout=120)
            data = r.json()
            img = ((data.get("result") or {}).get("image")) if r.ok else None
            if img:
                path.write_bytes(base64.b64decode(img))
                return path
            log(f"cloudflare essai {attempt+1} :", r.status_code, str(data.get("errors"))[:200])
        except Exception as e:
            log(f"cloudflare essai {attempt+1} :", str(e)[:200])
        time.sleep(5 * (attempt + 1))
    return None


def comic_image(prompt, path, seed):
    """Case de BD : Cloudflare si configuré, sinon Pollinations (anonyme : filigrane possible)."""
    if os.environ.get("OFFLINE"):
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
             f"mandelbrot=s=1024x1024:start_scale={1.5 + (seed % 7) * 0.3}", "-frames:v", "1", str(path)])
        return path
    full = f"{prompt}. {COMIC_STYLE}"
    if CF_ACCOUNT and CF_TOKEN:
        got = cloudflare_image(full, path, seed)
        if got:
            return got
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(full, safe="")
           + f"?width=1024&height=1280&model=flux&nologo=true&seed={seed}")
    headers = {"Authorization": f"Bearer {POLLI_TOKEN}"} if POLLI_TOKEN else {}
    for attempt in range(2 if not POLLI_TOKEN else 4):
        wait = 16 - (time.time() - _last_polli[0])
        if wait > 0:
            time.sleep(wait)
        _last_polli[0] = time.time()
        try:
            return download(url, path, timeout=150, headers=headers)
        except Exception as e:
            log(f"pollinations essai {attempt+1} :", str(e)[:160])
            time.sleep(10 * (attempt + 1))
    return None


def paper_background(path):
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={PAPER}:s={W}x{H}:d=1",
         "-vf", "noise=alls=7:allf=u,vignette=PI/5", "-frames:v", "1", str(path)])
    return path


def make_panel_still(img, bg, out):
    """Case de BD : image recadrée 4:5, bord noir épais, ombre portée, sur papier."""
    pw, ph = PANEL_W - 2 * BORDER, PANEL_H - 2 * BORDER
    fc = (
        f"[1:v]scale={pw}:{ph}:force_original_aspect_ratio=increase,crop={pw}:{ph},"
        f"pad={PANEL_W}:{PANEL_H}:{BORDER}:{BORDER}:black[p];"
        f"[0:v]drawbox=x={PANEL_X+14}:y={PANEL_Y+16}:w={PANEL_W}:h={PANEL_H}:"
        f"color=black@0.35:t=fill[b];"
        f"[b][p]overlay={PANEL_X}:{PANEL_Y},format=yuv420p"
    )
    run(["ffmpeg", "-y", "-i", str(bg), "-i", str(img), "-filter_complex", fc,
         "-frames:v", "1", "-q:v", "2", str(out)])


def placeholder(path, text_color="0x8a4b1c"):
    run(["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"gradients=s=1024x1280:c0=0x3b1f0e:c1={text_color}:x0=0:y0=0:x1=1024:y1=1280:d=1",
         "-frames:v", "1", str(path)])
    return path


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
    events = []
    groups, chunk = [], []
    for w in words:
        chunk.append(w)
        if len(chunk) >= 6 or re.search(r"[.,;:!?…]$", w[0]):
            groups.append(chunk)
            chunk = []
    if chunk:
        groups.append(chunk)
    for i, g in enumerate(groups):
        start = g[0][1]
        end = groups[i + 1][0][1] if i + 1 < len(groups) else g[-1][2] + 0.3
        end = min(end, g[-1][2] + 0.6)
        txt = " ".join(x[0] for x in g)
        events.append(f"Dialogue: 1,{ass_time(start)},{ass_time(end)},Recit,,0,0,0,,"
                      + wrap(ass_escape(txt), 28))
    events.append(f"Dialogue: 0,{ass_time(0)},{ass_time(total)},Label,,0,0,0,,{ass_escape(label)}")
    events.append(f"Dialogue: 2,{ass_time(0.2)},{ass_time(min(6, total))},Title,,0,0,0,,"
                  r"{\fad(300,400)}" + wrap(ass_escape(title.upper()), 20))
    if teaser:
        events.append(f"Dialogue: 2,{ass_time(max(0, total - 7))},{ass_time(total)},Teaser,,0,0,0,,"
                      r"{\fad(400,0)}" + wrap(ass_escape("DEMAIN : " + teaser), 24))

    # couleurs ASS : &HAABBGGRR
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Recit,{body_font},54,&H00141414,&H00141414,&H0078E8FF,&H00000000,1,0,0,0,100,100,0,0,3,16,4,2,90,90,330,1
Style: Title,{title_font},96,&H0000C8FF,&H00FFFFFF,&H00141414,&H96000000,1,0,0,0,100,100,1,0,1,7,4,8,60,60,300,1
Style: Label,{body_font},36,&H00C3E4F1,&H00FFFFFF,&H00141414,&H00000000,1,0,0,0,100,100,4,0,3,12,0,8,60,60,120,1
Style: Teaser,{title_font},70,&H00FFFFFF,&H00FFFFFF,&H00103A6B,&H00000000,1,0,0,0,100,100,0,0,3,20,0,5,80,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def pick_fonts():
    title = "Anton" if (FONT_DIR / "Anton-Regular.ttf").exists() else "DejaVu Sans"
    return title, "DejaVu Sans"


# ---------------------------------------------------------------- vidéo
MOVES = [
    ("min(1+0.00045*on,1.07)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    ("min(1.03+0.0003*on,1.08)", "(iw-iw/zoom)*on/{F}", "ih/2-(ih/zoom/2)"),
    ("max(1.07-0.00045*on,1.0)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    ("min(1.03+0.0003*on,1.08)", "(iw-iw/zoom)*(1-on/{F})", "ih/2-(ih/zoom/2)"),
]


def make_segment(still, audio, dur, idx, out):
    frames = int(math.ceil(dur * FPS))
    z, x, y = MOVES[idx % len(MOVES)]
    x = x.replace("{F}", str(frames))
    vf = (f"scale={W*2}:{H*2},zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},"
          f"fade=t=in:st=0:d=0.3,fade=t=out:st={dur-0.3:.2f}:d=0.3,format=yuv420p")
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
    scenes = get_scenes(ep)
    episode = str(ep.get("episode") or ep.get("current_episode") or "").strip()
    label = f"{SERIES_LABEL} · ÉP. {episode}" if episode else SERIES_LABEL
    teaser = (ep.get("next_event") or "").strip()
    seed = int(re.sub(r"\D", "", episode) or 7) * 101 + 7
    log(f"épisode {episode} — {title} — {len(scenes)} scènes")

    bg = paper_background(work / "paper.png")
    used_urls, images_info = [], []

    # images : scène 1 = palais d'Abomey (photo), puis cases de BD
    stills = []
    for i, (_, prompt) in enumerate(scenes):
        img = None
        if i == 0:
            img, u = wikimedia_file(PALACE_QUERIES, work / "img0.jpg")
            if u:
                used_urls.append(u)
                images_info.append({"scene": 1, "source": "wikimedia", "url": u})
        else:
            p = prompt or scenes[i][0][:300]
            img = comic_image(p, work / f"img{i}.jpg", seed + i)
            if img:
                images_info.append({"scene": i + 1, "source": "pollinations", "prompt": p})
            else:  # repli : photo d'archive
                img, u = wikimedia_file([ep.get("image_query"), *FALLBACK_QUERIES],
                                        work / f"img{i}.jpg", skip=used_urls)
                if u:
                    used_urls.append(u)
                    images_info.append({"scene": i + 1, "source": "wikimedia", "url": u})
        if not img:
            img = placeholder(work / f"img{i}.png")
        still = work / f"still{i}.jpg"
        make_panel_still(img, bg, still)
        stills.append(still)
        log(f"image {i+1}/{len(scenes)} prête")

    all_words, segments, offset = [], [], 0.0
    for i, (text, _) in enumerate(scenes):
        mp3 = work / f"voice{i}.mp3"
        words = align_punctuation(tts(text, mp3), text)
        last = i == len(scenes) - 1
        dur = probe_duration(mp3) + (1.4 if last else 0.5)
        if last and teaser:
            dur = max(dur, 7.5)
        seg = work / f"seg{i}.mp4"
        make_segment(stills[i], mp3, dur, i, seg)
        for w, s, e in words:
            all_words.append([w, s + offset, e + offset])
        segments.append(seg)
        offset += dur
        log(f"scène {i+1}: {dur:.1f}s")

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
    run(["ffmpeg", "-y", "-ss", "2", "-i", str(video), "-frames:v", "1", "-q:v", "3",
         str(out / "cover.jpg")])

    source = (ep.get("source") or "").strip()
    desc = f"{title}\n\n"
    if episode:
        desc += f"Épisode {episode} de notre série sur l'histoire du royaume du Dahomey.\n"
    if teaser:
        desc += f"Demain : {teaser}\n"
    if source:
        desc += f"\nSources : {source}\n"
    desc += "Illustrations : photo Wikimedia Commons + dessins générés par IA.\n"
    desc += f"\n{HASHTAGS}"
    (out / "description.txt").write_text(desc, encoding="utf-8")

    meta = {k: ep.get(k) for k in ("episode", "title", "next_episode", "next_period",
                                   "next_event", "source", "source_url")}
    meta.update(duration=round(total, 1), images=images_info, description=desc)
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"OK {video} ({total:.1f}s)")


# ======================================================================
#  Mode « motion comic » (par défaut)
# ======================================================================
def get_scenes_motion(ep):
    """[(narration, [ {image_prompt, bubble, sfx}, ... ])] — 1 à 3 cases par scène."""
    raw = ep.get("scenes")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    out = []
    if isinstance(raw, list):
        for s in raw:
            if not isinstance(s, dict):
                continue
            txt = (s.get("narration") or s.get("text") or "").strip()
            if not txt:
                continue
            panels = []
            for p in (s.get("panels") or [])[:3]:
                if isinstance(p, dict):
                    panels.append({
                        "image_prompt": (p.get("image_prompt") or "").strip(),
                        "bubble": (p.get("bubble") or "").strip()[:70] or None,
                        "sfx": (p.get("sfx") or "").strip()[:12] or None,
                    })
                elif isinstance(p, str) and p.strip():
                    panels.append({"image_prompt": p.strip(), "bubble": None, "sfx": None})
            if not panels:
                panels = [{"image_prompt": (s.get("image_prompt") or "").strip(),
                           "bubble": None, "sfx": None}]
            out.append((txt, panels))
    if len(out) >= 2:
        return out
    return [(t, [{"image_prompt": ip, "bubble": None, "sfx": None}]) for t, ip in get_scenes(ep)]


def caption_groups(words, max_words=6):
    groups, chunk = [], []
    for w in words:
        chunk.append(w)
        if len(chunk) >= max_words or re.search(r"[.,;:!?…]$", w[0]):
            groups.append(chunk)
            chunk = []
    if chunk:
        groups.append(chunk)
    caps = []
    for i, g in enumerate(groups):
        start = g[0][1]
        end = groups[i + 1][0][1] if i + 1 < len(groups) else g[-1][2] + 0.3
        end = min(end, g[-1][2] + 0.6)
        caps.append((start, end, " ".join(x[0] for x in g)))
    return caps


def main_motion(payload_path, out_dir):
    from PIL import Image
    import motion
    import sound

    ep = json.loads(Path(payload_path).read_text(encoding="utf-8"))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="motion_"))

    title = (ep.get("title") or "Histoire du Dahomey").strip()
    scenes = get_scenes_motion(ep)
    episode = str(ep.get("episode") or ep.get("current_episode") or "").strip()
    label = f"{SERIES_LABEL} · ÉP. {episode}" if episode else SERIES_LABEL
    teaser = (ep.get("next_event") or "").strip()
    seed = int(re.sub(r"\D", "", episode) or 7) * 101 + 7
    log(f"épisode {episode} — {title} — {len(scenes)} scènes, "
        f"{sum(len(p) for _, p in scenes)} cases (motion comic)")

    # 1) voix et timings
    voice_files, durs, words_all = [], [], []
    for i, (text, _) in enumerate(scenes):
        mp3 = work / f"voice{i}.mp3"
        words = align_punctuation(tts(text, mp3), text)
        last = i == len(scenes) - 1
        d = probe_duration(mp3) + (1.6 if last else 0.7)
        if last and teaser:
            d = max(d, 8.0)
        voice_files.append(mp3)
        durs.append(d)
        words_all.append(words)

    # 2) images : scène 1 = photo du palais, ensuite cases de BD
    used_urls, images_info = [], []
    built, start = [], 0.0
    whooshes, impacts = [], [0.55]
    k = 0
    for i, ((text, panels), d, words) in enumerate(zip(scenes, durs, words_all)):
        if i == 0:
            panels = panels[:1]
        n = len(panels)
        rects = motion.layout(n)
        mp = []
        for j, pinfo in enumerate(panels):
            path = work / f"img{i}_{j}.jpg"
            img = None
            if i == 0:
                img, u = wikimedia_file(PALACE_QUERIES, path)
                if u:
                    images_info.append({"scene": 1, "source": "wikimedia", "url": u})
            else:
                prompt = pinfo["image_prompt"] or text[:300]
                img = comic_image(prompt, path, seed + k)
                if img:
                    images_info.append({"scene": i + 1, "panel": j + 1, "source": "cloudflare/pollinations",
                                        "prompt": prompt})
                else:
                    img, u = wikimedia_file([ep.get("image_query"), *FALLBACK_QUERIES], path, skip=used_urls)
                    if u:
                        used_urls.append(u)
            if not img:
                img = placeholder(work / f"img{i}_{j}.png")
            k += 1
            # apparition calée sur la narration
            if j == 0:
                reveal = 0.15 if i else 0.0
            else:
                wi = min(len(words) - 1, (j * len(words)) // n) if words else 0
                reveal = max(0.6 * j, (words[wi][1] if words else d * j / n) - 0.15)
            pnl = motion.Panel(Image.open(img), rects[j], reveal, j,
                               bubble=pinfo.get("bubble"), sfx=pinfo.get("sfx"))
            mp.append(pnl)
            whooshes.append(start + reveal)
            if pnl.sfx is not None:
                impacts.append(start + pnl.sfx_t)
        kind = "embers" if i % 2 == 0 else "dust"
        built.append(motion.Scene(start, d, mp, motion.Particles(seed + i, kind)))
        if i:
            whooshes.append(start)
        start += d
        log(f"scène {i+1}: {d:.1f}s, {n} case(s)")
    total = start

    # 3) sous-titres (récitatifs)
    caps, off = [], 0.0
    for words, d in zip(words_all, durs):
        caps += [(s + off, e + off, t) for s, e, t in caption_groups(words)]
        off += d

    # 4) bande son : voix + musique (baissée sous la voix) + bruitages
    concat = work / "voice_list.txt"
    padded = []
    for i, (vf, d) in enumerate(zip(voice_files, durs)):
        pf = work / f"vpad{i}.wav"
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(vf), "-af",
             f"aresample=44100,apad,atrim=0:{d:.3f}", "-ac", "1", "-ar", "44100", str(pf)])
        padded.append(pf)
    concat.write_text("".join(f"file '{p}'\n" for p in padded))
    voice = work / "voice.wav"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
         "-c", "pcm_s16le", str(voice)])
    sound.write_wav(work / "music.wav", sound.music(total))
    sound.write_wav(work / "sfx.wav", sound.sfx_track(total, whooshes, impacts))
    mix = work / "mix.wav"
    run(["ffmpeg", "-y", "-loglevel", "error",
         "-i", str(voice), "-i", str(work / "music.wav"), "-i", str(work / "sfx.wav"),
         "-filter_complex",
         "[0:a]asplit=2[v][vk];"
         "[1:a]volume=0.32[m];"
         "[m][vk]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[md];"
         "[2:a]volume=0.55[s];"
         "[v][md][s]amix=inputs=3:normalize=0:duration=first,loudnorm=I=-14:TP=-1.5:LRA=11[a]",
         "-map", "[a]", "-ac", "2", "-ar", "44100", str(mix)])

    # 5) images animées
    comic = motion.MotionComic(built, caps, title, episode, label, teaser, total)
    video = out / "video.mp4"
    comic.render(video, mix)
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1.2", "-i", str(video),
         "-frames:v", "1", "-q:v", "3", str(out / "cover.jpg")])

    source = (ep.get("source") or "").strip()
    desc = f"{title}\n\n"
    if episode:
        desc += f"Épisode {episode} de notre série en BD sur l'histoire du royaume du Dahomey.\n"
    if teaser:
        desc += f"Demain : {teaser}\n"
    if source:
        desc += f"\nSources : {source}\n"
    desc += "Illustrations : photo Wikimedia Commons + dessins générés par IA. Musique originale.\n"
    desc += f"\n{HASHTAGS}"
    (out / "description.txt").write_text(desc, encoding="utf-8")
    meta = {k2: ep.get(k2) for k2 in ("episode", "title", "next_episode", "next_period",
                                      "next_event", "source", "source_url")}
    meta.update(duration=round(total, 1), images=images_info, description=desc)
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"OK {video} ({total:.1f}s)")


if __name__ == "__main__":
    if os.environ.get("RENDER_MODE", "motion") == "static":
        main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "out")
    else:
        main_motion(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "out")
