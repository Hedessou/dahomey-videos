#!/usr/bin/env python3
"""Construit payload.json à partir de l'événement GitHub (Make) ou d'une saisie manuelle."""
import json
import os
import re
import sys
from pathlib import Path


def loads_loose(text):
    """Accepte du JSON entouré de ```json … ``` ou de texte parasite."""
    if isinstance(text, dict):
        return text
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else {}


client = loads_loose(os.environ.get("CLIENT_PAYLOAD") or "{}") or {}
manual = os.environ.get("INPUT_PAYLOAD") or ""

if client:
    ep = client.get("ep") or {}
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
