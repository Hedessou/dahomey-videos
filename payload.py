#!/usr/bin/env python3
"""Construit payload.json à partir de l'événement GitHub (Make) ou d'une saisie manuelle."""
import json
import os
import re
import sys
from pathlib import Path


def repair_json(text, rounds=6):
    """Répare les petites erreurs de crochets/accolades de l'IA (inversion, oubli, en trop)."""
    for _ in range(rounds):
        try:
            return json.loads(text, strict=False)
        except json.JSONDecodeError as e:
            pos = e.pos
        fixed = None
        lo, hi = max(0, pos - 16), min(len(text), pos + 3)
        cands = []
        for i in range(lo, hi):
            c = text[i]
            if c in "]}":
                cands.append(text[:i] + ("}" if c == "]" else "]") + text[i + 1:])
                cands.append(text[:i] + text[i + 1:])
                if i + 1 < len(text) and text[i + 1] in "]}" and text[i + 1] != c:
                    cands.append(text[:i] + text[i + 1] + c + text[i + 2:])
            for ins in "]}":
                cands.append(text[:i] + ins + text[i:])
        best = None
        for c in cands:
            try:
                return json.loads(c, strict=False)
            except json.JSONDecodeError as e2:
                if best is None or e2.pos > best[0]:
                    best = (e2.pos, c)
        if best is None or best[0] <= pos:
            break
        text = best[1]
    raise json.JSONDecodeError("irréparable", text, 0)


def loads_loose(text):
    """Accepte du JSON entouré de ```json … ```, de texte parasite, de virgules en trop."""
    if isinstance(text, dict):
        return text
    text = (text or "").strip().lstrip("\ufeff")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.S)
    cands = [text] + ([m.group(0)] if m else [])
    for c in cands:
        for fix in (c, re.sub(r",\s*([}\]])", r"\1", c),
                    c.replace("\u201c", "«").replace("\u201d", "»")):
            try:
                return json.loads(fix, strict=False)
            except json.JSONDecodeError:
                pass
    for c in cands:
        try:
            return repair_json(c)
        except json.JSONDecodeError:
            pass
    sys.stderr.write("JSON illisible :\n" + text[:3000] + "\n")
    return {}


client = loads_loose(os.environ.get("CLIENT_PAYLOAD") or "{}") or {}
manual = os.environ.get("INPUT_PAYLOAD") or ""

if client:
    ep = client.get("ep") or {}
    if client.get("ep_b64"):
        import base64
        raw = base64.b64decode(client["ep_b64"]).decode("utf-8", "replace")
        ep = loads_loose(raw)
    if isinstance(ep, str):
        ep = loads_loose(ep)
    # numéro d'épisode courant venant du Google Sheet
    if client.get("current_episode") and not ep.get("episode"):
        ep["episode"] = client["current_episode"]
    if client.get("current_period") and not ep.get("period"):
        ep["period"] = client["current_period"]
elif manual.strip():
    ep = loads_loose(manual)
else:
    ep = json.loads(Path("exemples/episode.json").read_text(encoding="utf-8"))

if not ep.get("script") and not ep.get("scenes"):
    sys.exit("Payload sans script : rien à rendre.")
json.dump(ep, sys.stdout, ensure_ascii=False, indent=2)
