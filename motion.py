"""
Moteur « motion comic » : planches de BD animées, image par image (Pillow + numpy),
envoyées directement à ffmpeg.

Une scène = une planche de 1 à 3 cases qui apparaissent au rythme de la narration.
Effets : apparition des cases avec rebond, caméra qui glisse vers la case active,
mouvement lent dans chaque image, bulles et onomatopées qui « pop », secousse
d'impact, braises/poussière, titre en impact, page qui tourne entre les scènes,
récitatifs synchronisés à la voix, bandeau « DEMAIN » à la fin.
"""
import math
import random
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H, FPS = 1080, 1920, 30
PAGE = (36, 210, 1044, 1500)        # zone de la planche (x0, y0, x1, y1)
GUT = 22                            # gouttière entre les cases
BORDER = 7
FONT_DIR = Path(__file__).parent / "fonts"
DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

PAPER = (241, 228, 195)
INK = (20, 20, 20)
YELLOW = (255, 214, 0)
RED = (196, 32, 38)
RECIT = (255, 233, 122)


# ----------------------------------------------------------------- utilitaires
def font(name, size):
    for p in (FONT_DIR / name, Path(DEJAVU)):
        try:
            return ImageFont.truetype(str(p), size)
        except OSError:
            continue
    return ImageFont.load_default()


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def ease_out_back(x, s=1.70158):
    x = clamp(x) - 1
    return 1 + (s + 1) * x ** 3 + s * x ** 2


def ease_in_out(x):
    x = clamp(x)
    return x * x * (3 - 2 * x)


def wrap_lines(draw, text, fnt, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=fnt) <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def cover(img, w, h):
    """Recadre une image pour remplir w x h sans déformation."""
    iw, ih = img.size
    s = max(w / iw, h / ih)
    img = img.resize((max(w, int(iw * s + 1)), max(h, int(ih * s + 1))), Image.LANCZOS)
    iw, ih = img.size
    l, t = (iw - w) // 2, (ih - h) // 2
    return img.crop((l, t, l + w, t + h))


def layout(n):
    x0, y0, x1, y1 = PAGE
    w, h = x1 - x0, y1 - y0
    if n <= 1:
        return [(x0, y0, w, h)]
    if n == 2:
        hh = (h - GUT) // 2
        return [(x0, y0, w, hh), (x0, y0 + hh + GUT, w, h - hh - GUT)]
    th = int(h * 0.5)
    bw = (w - GUT) // 2
    return [(x0, y0, w, th),
            (x0, y0 + th + GUT, bw, h - th - GUT),
            (x0 + bw + GUT, y0 + th + GUT, w - bw - GUT, h - th - GUT)]


# ----------------------------------------------------------------- éléments graphiques
def paper_background():
    """Papier crème avec trame de points (halftone) et vignettage."""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    base = np.ones((H, W, 3), np.float32) * np.array(PAPER, np.float32)
    # trame de points
    step = 14
    cx = (xx % step) - step / 2
    cy = (yy % step) - step / 2
    d = np.sqrt(cx ** 2 + cy ** 2)
    grad = 1.6 + 2.4 * (yy / H)
    dots = (d < grad).astype(np.float32) * 0.07
    base *= (1 - dots[..., None])
    # vignettage
    r = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2)
    base *= (1 - 0.28 * clamp_arr(r - 0.55, 0, 1)[..., None])
    noise = np.random.default_rng(3).normal(0, 3.5, (H, W, 1)).astype(np.float32)
    return Image.fromarray(np.clip(base + noise, 0, 255).astype(np.uint8))


def clamp_arr(a, lo, hi):
    return np.minimum(np.maximum(a, lo), hi)


def make_bubble(text, max_w=420):
    """Bulle de dialogue blanche avec contour noir et queue."""
    f = font("ComicNeue-Bold.ttf", 40)
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    lines = wrap_lines(tmp, text, f, max_w)
    lh = 46
    tw = int(max(tmp.textlength(l, font=f) for l in lines))
    th = lh * len(lines)
    pw, ph = tw + 70, th + 56
    img = Image.new("RGBA", (pw + 20, ph + 60), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # queue
    d.polygon([(pw * 0.30, ph - 12), (pw * 0.22, ph + 52), (pw * 0.48, ph - 10)],
              fill="white", outline=INK)
    d.line([(pw * 0.30, ph - 6), (pw * 0.22, ph + 52), (pw * 0.48, ph - 6)], fill=INK, width=5)
    d.ellipse((4, 4, pw, ph), fill="white", outline=INK, width=6)
    # recouvre la base de la queue
    d.polygon([(pw * 0.31, ph - 16), (pw * 0.25, ph + 40), (pw * 0.45, ph - 14)], fill="white")
    y = (ph - th) // 2 + 2
    for l in lines:
        lw = d.textlength(l, font=f)
        d.text(((pw - lw) / 2 + 2, y), l, font=f, fill=INK)
        y += lh
    return img


def make_sfx(text):
    """Onomatopée façon BD : lettres épaisses, jaune, contour noir, ombre rouge."""
    f = font("Bangers-Regular.ttf", 150)
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    tw = int(tmp.textlength(text, font=f)) + 80
    img = Image.new("RGBA", (tw, 230), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.text((48, 38), text, font=f, fill=RED, stroke_width=12, stroke_fill=INK)
    d.text((36, 26), text, font=f, fill=YELLOW, stroke_width=10, stroke_fill=INK)
    return img.rotate(9, expand=True, resample=Image.BICUBIC)


def make_title(title, episode):
    f = font("Bangers-Regular.ttf", 112)
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    lines = wrap_lines(tmp, title.upper(), f, 900)[:3]
    lh = 118
    img = Image.new("RGBA", (W, 150 + lh * len(lines)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if episode:
        bf = font("Bangers-Regular.ttf", 54)
        badge = f"ÉPISODE {episode}"
        bw = d.textlength(badge, font=bf) + 50
        d.rounded_rectangle(((W - bw) / 2, 8, (W + bw) / 2, 88), 16, fill=RED, outline=INK, width=5)
        d.text(((W - bw) / 2 + 25, 18), badge, font=bf, fill="white")
    y = 112
    for l in lines:
        lw = d.textlength(l, font=f)
        d.text(((W - lw) / 2 + 7, y + 7), l, font=f, fill=RED, stroke_width=10, stroke_fill=INK)
        d.text(((W - lw) / 2, y), l, font=f, fill=YELLOW, stroke_width=9, stroke_fill=INK)
        y += lh
    return img


def make_label(label):
    f = font("Bangers-Regular.ttf", 40)
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    tw = int(tmp.textlength(label, font=f))
    img = Image.new("RGBA", (tw + 48, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, tw + 46, 62), 14, fill=(20, 20, 20, 225))
    d.text((23, 10), label, font=f, fill=(255, 233, 160))
    return img


def make_recit(text):
    f = font("ComicNeue-Bold.ttf", 50)
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    lines = wrap_lines(tmp, text, f, 900)
    lh = 58
    tw = int(max(tmp.textlength(l, font=f) for l in lines))
    img = Image.new("RGBA", (tw + 56, lh * len(lines) + 34), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((8, 8, tw + 54, lh * len(lines) + 32), fill=(0, 0, 0, 120))
    d.rectangle((0, 0, tw + 46, lh * len(lines) + 24), fill=RECIT, outline=INK, width=5)
    y = 12
    for l in lines:
        d.text((23, y), l, font=f, fill=INK)
        y += lh
    return img


def make_teaser(text):
    f1 = font("Bangers-Regular.ttf", 64)
    f2 = font("ComicNeue-Bold.ttf", 46)
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    lines = wrap_lines(tmp, text, f2, 860)[:3]
    h = 100 + 54 * len(lines)
    img = Image.new("RGBA", (W - 60, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((10, 10, W - 60, h), fill=(0, 0, 0, 120))
    d.rectangle((0, 0, W - 70, h - 10), fill=RED, outline=INK, width=6)
    d.text((30, 12), "DEMAIN…", font=f1, fill=YELLOW, stroke_width=4, stroke_fill=INK)
    y = 88
    for l in lines:
        d.text((32, y), l, font=f2, fill="white")
        y += 54
    return img


def paste_scaled(base, tile, cx, cy, scale=1.0, alpha=1.0, angle=0.0):
    """Colle une image RGBA centrée en (cx, cy) avec échelle, opacité et rotation."""
    if scale <= 0.02 or alpha <= 0.01:
        return
    t = tile
    if abs(scale - 1) > 0.005:
        t = t.resize((max(1, int(t.width * scale)), max(1, int(t.height * scale))), Image.BILINEAR)
    if abs(angle) > 0.2:
        t = t.rotate(angle, expand=True, resample=Image.BILINEAR)
    if alpha < 0.999:
        a = t.getchannel("A").point(lambda v: int(v * alpha))
        t = t.copy()
        t.putalpha(a)
    base.alpha_composite(t, (int(cx - t.width / 2), int(cy - t.height / 2)))


# ----------------------------------------------------------------- particules
class Particles:
    def __init__(self, seed, kind="embers", n=70):
        rnd = random.Random(seed)
        self.kind = kind
        self.p = []
        for _ in range(n):
            self.p.append(dict(
                x=rnd.uniform(0, W), y=rnd.uniform(0, H), r=rnd.uniform(2, 6),
                vy=rnd.uniform(-60, -18), ph=rnd.uniform(0, 6.28), sw=rnd.uniform(10, 40),
                tw=rnd.uniform(1.5, 4.0)))

    def draw(self, layer, t):
        d = ImageDraw.Draw(layer)
        for q in self.p:
            y = (q["y"] + q["vy"] * t) % (H + 40) - 20
            x = q["x"] + math.sin(t * 0.9 + q["ph"]) * q["sw"]
            a = 0.45 + 0.55 * math.sin(t * q["tw"] + q["ph"]) ** 2
            if self.kind == "embers":
                col = (255, int(140 + 80 * a), 40, int(150 * a))
            else:
                col = (255, 240, 210, int(90 * a))
            r = q["r"]
            d.ellipse((x - r, y - r, x + r, y + r), fill=col)


# ----------------------------------------------------------------- planche
class Panel:
    def __init__(self, img, rect, reveal, idx, bubble=None, sfx=None):
        x, y, w, h = rect
        self.rect = rect
        self.reveal = reveal
        self.idx = idx
        iw, ih = w - 2 * BORDER, h - 2 * BORDER
        self.inner = (iw, ih)
        self.big = cover(img.convert("RGB"), int(iw * 1.16), int(ih * 1.16))
        self.bubble = make_bubble(bubble) if bubble else None
        self.sfx = make_sfx(sfx.upper()) if sfx else None
        self.sfx_t = reveal + 0.55
        self.bubble_t = reveal + 0.75
        self.pan = [(0, 0), (1, 0), (-1, 0), (0, 1)][idx % 4]

    def frame_tile(self, local_t, dur):
        """Image de la case (avec bord noir) à l'instant local_t."""
        iw, ih = self.inner
        bw, bh = self.big.size
        p = clamp(local_t / max(1.0, dur))
        z = 1.0 - 0.12 * ease_in_out(p)          # zoom avant lent
        cw, ch = bw * z, bh * z
        dx, dy = self.pan
        cx = bw / 2 + dx * (bw - cw) / 2 * (2 * p - 1) * 0.9
        cy = bh / 2 + dy * (bh - ch) / 2 * (2 * p - 1) * 0.9
        box = (cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2)
        inner = self.big.resize((iw, ih), Image.BILINEAR, box=box)
        tile = Image.new("RGBA", (iw + 2 * BORDER, ih + 2 * BORDER), INK + (255,))
        tile.paste(inner, (BORDER, BORDER))
        return tile


class Scene:
    def __init__(self, start, dur, panels, particles):
        self.start = start
        self.dur = dur
        self.panels = panels
        self.particles = particles


# ----------------------------------------------------------------- rendu
class MotionComic:
    def __init__(self, scenes, captions, title, episode, label, teaser, total):
        self.scenes = scenes
        self.captions = captions              # [(start, end, text)]
        self.total = total
        self.bg = paper_background().convert("RGBA")
        self.title = make_title(title, episode)
        self.label = make_label(label)
        self.teaser = make_teaser(teaser) if teaser else None
        self.recit_cache = {}
        self.cam = None                       # (zoom, cx, cy) lissés
        self.impacts = []                     # instants de secousse
        for s in scenes:
            for p in s.panels:
                if p.sfx:
                    self.impacts.append(s.start + p.sfx_t)
        self.title_impact = 0.55
        self.impacts.append(self.title_impact)

    # --- page d'une scène, dans le repère de la page
    def page(self, sc, lt):
        img = self.bg.copy()
        active = None
        d0 = ImageDraw.Draw(img)
        for p in sc.panels:
            if lt < p.reveal:
                # contour de la case à venir (page de BD pas encore remplie)
                x, y, w, h = p.rect
                d0.rectangle((x, y, x + w, y + h), fill=(255, 250, 235, 70),
                             outline=(60, 50, 40, 110), width=4)
                continue
            active = p
            x, y, w, h = p.rect
            a = lt - p.reveal
            e = ease_out_back(a / 0.45)
            scale = 0.55 + 0.45 * e
            alpha = clamp(a / 0.18)
            angle = (1 - clamp(a / 0.45)) * (6 if p.idx % 2 else -6)
            tile = p.frame_tile(a, sc.dur - p.reveal)
            # ombre portée
            if a > 0.3:
                sh = Image.new("RGBA", tile.size, (0, 0, 0, 90))
                img.alpha_composite(sh, (x + 12, y + 14))
            paste_scaled(img, tile, x + w / 2, y + h / 2, scale, alpha, angle)
        # bulles et onomatopées par-dessus
        for p in sc.panels:
            x, y, w, h = p.rect
            if p.bubble is not None and lt >= p.bubble_t:
                b = lt - p.bubble_t
                bx = x + (w * 0.30 if p.idx % 2 == 0 else w * 0.68)
                by = y + p.bubble.height / 2 + 26
                paste_scaled(img, p.bubble, bx, by, 0.3 + 0.7 * ease_out_back(b / 0.3), clamp(b / 0.12))
            if p.sfx is not None and p.sfx_t <= lt <= p.sfx_t + 1.6:
                b = lt - p.sfx_t
                sc_ = 1.0 + 0.9 * (1 - ease_out_back(b / 0.25))
                al = clamp(b / 0.08) * (1 - clamp((b - 1.25) / 0.35))
                paste_scaled(img, p.sfx, x + w * 0.62, y + h * 0.62, sc_, al, -4)
        return img, active

    def camera(self, active, t):
        if active is None:
            tz, tx, ty = 1.0, W / 2, H / 2
        else:
            x, y, w, h = active.rect
            # zoom seulement si la case (avec marge) tient encore entière à l'écran
            tz = max(1.0, min(1.14, W / (w + 90), H / (h + 700)))
            tx = W / 2 + (x + w / 2 - W / 2) * 0.6
            ty = H / 2 + (y + h / 2 - H / 2) * 0.45
        if self.cam is None:
            self.cam = [tz, tx, ty]
        k = 0.075
        self.cam[0] += (tz - self.cam[0]) * k
        self.cam[1] += (tx - self.cam[1]) * k
        self.cam[2] += (ty - self.cam[2]) * k
        z, cx, cy = self.cam
        # secousse
        sx = sy = 0.0
        for ti in self.impacts:
            d = t - ti
            if 0 <= d < 0.45:
                amp = 22 * math.exp(-d * 9)
                sx += amp * math.sin(d * 70)
                sy += amp * math.cos(d * 55)
        return z, cx + sx, cy + sy

    @staticmethod
    def apply_camera(img, z, cx, cy):
        cw, ch = W / z, H / z
        cx = min(max(cx, cw / 2), W - cw / 2) if z >= 1 else cx
        cy = min(max(cy, ch / 2), H - ch / 2) if z >= 1 else cy
        box = (cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2)
        return img.resize((W, H), Image.BILINEAR, box=box)

    def scene_at(self, t):
        for i, s in enumerate(self.scenes):
            if s.start <= t < s.start + s.dur:
                return i, s
        return len(self.scenes) - 1, self.scenes[-1]

    def overlay(self, frame, t, sc):
        # particules
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sc.particles.draw(layer, t)
        frame.alpha_composite(layer)
        # bandeau série
        frame.alpha_composite(self.label, ((W - self.label.width) // 2, 70))
        # titre en impact (début de vidéo)
        if t < 5.2:
            a = t - 0.25
            if a > 0:
                sc_ = 1 + 1.6 * (1 - ease_out_back(a / 0.32))
                al = clamp(a / 0.1) * (1 - clamp((t - 4.6) / 0.6))
                paste_scaled(frame, self.title, W / 2, 640, sc_, al)
        # récitatif synchronisé
        for (s, e, txt) in self.captions:
            if s <= t < e:
                img = self.recit_cache.get(txt)
                if img is None:
                    img = self.recit_cache[txt] = make_recit(txt)
                pop = 0.92 + 0.08 * ease_out_back((t - s) / 0.15)
                paste_scaled(frame, img, W / 2, 1630 + img.height / 2 - 40, pop)
                break
        # teaser de fin
        if self.teaser is not None and t > self.total - 7:
            a = t - (self.total - 7)
            x = -W + (W + 30) * ease_out_back(a / 0.5)
            frame.alpha_composite(self.teaser, (int(x + 30), 880))

    def render(self, out_path, audio_path, fps=FPS, preset="veryfast"):
        n = int(math.ceil(self.total * fps))
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
               "-i", str(audio_path),
               "-map", "0:v", "-map", "1:a",
               "-c:v", "libx264", "-preset", preset, "-crf", "21", "-pix_fmt", "yuv420p",
               "-profile:v", "high", "-c:a", "aac", "-b:a", "192k", "-shortest",
               "-movflags", "+faststart", str(out_path)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        prev_scene, prev_frame, trans_from = -1, None, None
        for i in range(n):
            t = i / fps
            si, sc = self.scene_at(t)
            lt = t - sc.start
            if si != prev_scene:
                trans_from = prev_frame
                prev_scene = si
            page, active = self.page(sc, lt)
            z, cx, cy = self.camera(active, t)
            frame = self.apply_camera(page, z, cx, cy)
            # page qui tourne : la nouvelle planche glisse sur l'ancienne
            if trans_from is not None and lt < 0.5:
                e = ease_in_out(lt / 0.5)
                base = trans_from.copy()
                dark = Image.new("RGBA", (W, H), (0, 0, 0, int(110 * e)))
                base.alpha_composite(dark)
                xoff = int(W * (1 - e))
                shadow = Image.new("RGBA", (40, H), (0, 0, 0, 90))
                base.alpha_composite(frame.crop((0, 0, W - xoff, H)) if xoff < W else frame.crop((0, 0, 1, H)),
                                     (xoff, 0))
                if 0 < xoff < W - 40:
                    base.alpha_composite(shadow, (max(0, xoff - 40), 0))
                frame = base
            self.overlay(frame, t, sc)
            prev_frame = frame
            proc.stdin.write(frame.convert("RGB").tobytes())
            if i % (fps * 10) == 0:
                print(f"[motion] {t:5.1f}s / {self.total:.1f}s", flush=True)
        proc.stdin.close()
        if proc.wait() != 0:
            raise RuntimeError("ffmpeg (motion) a échoué")
