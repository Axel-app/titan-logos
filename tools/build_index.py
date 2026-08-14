#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Génère `index_v3.tsv` à partir de plusieurs bases publiques de logos.

Aucune image n'est copiée : l'index ne contient que des liens vers les
hébergeurs d'origine.

Colonnes (séparées par une tabulation) :
    searchKey  nameKey  name  country  url  source

Usage :
    python3 tools/build_index.py            # écrit index_v3.tsv
    python3 tools/build_index.py --dry-run  # n'écrit rien
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request

USER_AGENT = "titan-logos-index/1"
OUT_NAME = "index_v3.tsv"

SKIP_EXTENSIONS = (".svg",)
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
MAX_VARIANTS_PER_CHANNEL = 3


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read()


def fetch_json(url: str):
    return json.loads(fetch(url))


def normalize(text: str) -> str:
    """Minuscules, sans accents, ponctuation réduite à des espaces."""
    text = unicodedata.normalize("NFD", text or "")
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def github_tree(repo: str, branch: str) -> list[str]:
    """Chemins des fichiers d'un dépôt (jeton facultatif : lève la limite d'appels)."""
    url = f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if token := os.environ.get("GITHUB_TOKEN"):
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=180) as resp:
        tree = json.loads(resp.read())
    if tree.get("truncated"):
        raise SystemExit(f"{repo}: arbre tronqué par GitHub, index incomplet")
    return [t["path"] for t in tree["tree"] if t["type"] == "blob"]


# ── Sources ─────────────────────────────────────────────────────────

def source_iptv_org() -> list[tuple]:
    logos = fetch_json("https://iptv-org.github.io/api/logos.json")
    channels = {c["id"]: c for c in fetch_json("https://iptv-org.github.io/api/channels.json")}

    # Les variantes encore en circulation d'abord (les autres finissent en 404).
    # Tri stable : l'ordre du fichier départage, l'index reste reproductible.
    ordered = sorted(logos, key=lambda e: not e.get("in_use"))

    by_channel: dict[str, list[str]] = {}
    for entry in ordered:
        url = entry.get("url")
        if not url or url.lower().endswith(SKIP_EXTENSIONS):
            continue
        bucket = by_channel.setdefault(entry["channel"], [])
        if len(bucket) < MAX_VARIANTS_PER_CHANNEL and url not in bucket:
            bucket.append(url)

    rows = []
    for channel_id, urls in by_channel.items():
        channel = channels.get(channel_id)
        if not channel:
            continue
        name = channel.get("name") or channel_id
        aliases = " ".join(channel.get("alt_names") or [])
        country = (channel.get("country") or "").lower()
        search_key = normalize(f"{name} {aliases} {country}")
        name_key = normalize(name)
        for url in urls:
            rows.append((search_key, name_key, name, country, url, "i"))
    return rows


def source_tv_logos() -> list[tuple]:
    """Chemins de la forme `countries/<pays>/<slug>-<cc>.png`."""
    rows = []
    for path in github_tree("tv-logo/tv-logos", "main"):
        if not path.startswith("countries/") or not path.lower().endswith(IMAGE_EXTENSIONS):
            continue
        slug = os.path.splitext(os.path.basename(path))[0]
        parts = slug.rsplit("-", 1)
        if len(parts) == 2 and 2 <= len(parts[1]) <= 3:
            name_slug, country = parts
        else:
            name_slug, country = slug, ""
        name = name_slug.replace("-", " ").title()
        url = "https://cdn.jsdelivr.net/gh/tv-logo/tv-logos@main/" + urllib.parse.quote(path)
        rows.append((normalize(f"{name} {country}"), normalize(name), name, country, url, "t"))
    return rows


def source_picons() -> list[tuple]:
    """Chemins de la forme `build-source/logos/<slug>.<variante>.<ext>`."""
    rows: list[tuple] = []
    seen: dict[str, int] = {}
    for path in github_tree("picons/picons", "master"):
        if "/logos/" not in path or not path.lower().endswith(IMAGE_EXTENSIONS):
            continue
        slug = os.path.basename(path).split(".")[0]
        if seen.get(slug, 0) >= MAX_VARIANTS_PER_CHANNEL:
            continue
        seen[slug] = seen.get(slug, 0) + 1
        name = slug.replace("-", " ").title()
        url = "https://cdn.jsdelivr.net/gh/picons/picons@master/" + urllib.parse.quote(path)
        rows.append((normalize(name), normalize(name), name, "", url, "p"))
    return rows


SOURCES = [
    ("iptv-org", source_iptv_org),
    ("tv-logos", source_tv_logos),
    ("picons", source_picons),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="n'écrit aucun fichier")
    args = parser.parse_args()

    rows: list[tuple] = []
    for label, loader in SOURCES:
        try:
            produced = loader()
        except Exception as error:  # une source morte ne doit pas tuer l'index
            print(f"{label} : échec ({error}), source ignorée", file=sys.stderr)
            continue
        print(f"{label:10} : {len(produced):>7,} entrées")
        rows.extend(produced)

    if not rows:
        raise SystemExit("aucune source exploitable, index existant conservé")

    # Par (chaîne, URL) : un même fichier sert souvent plusieurs déclinaisons
    # d'une chaîne, qui doivent toutes rester trouvables.
    unique, seen = [], set()
    for row in rows:
        key = (row[0], row[4])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)

    unique.sort(key=lambda r: (r[0], r[2], r[4]))

    payload = "".join("\t".join(field.replace("\t", " ").replace("\n", " ") for field in row) + "\n"
                      for row in unique)
    blob = payload.encode("utf-8")
    digest = hashlib.sha256(blob).hexdigest()[:16]

    print(f"\ntotal        : {len(unique):>7,} lignes ({len(rows) - len(unique):,} doublons écartés)")
    print(f"publié       : {len(blob) / 1024 / 1024:.1f} Mo")
    print(f"sur le fil   : {len(gzip.compress(blob, 9)) / 1024:.0f} ko")
    print(f"empreinte    : {digest}")

    if args.dry_run:
        print("\n--dry-run : rien écrit")
        return 0

    with open(OUT_NAME, "wb") as handle:
        handle.write(blob)
    with open("index.json", "w", encoding="utf-8") as handle:
        json.dump({"version": 3, "lines": len(unique), "sha256_16": digest,
                   "file": OUT_NAME}, handle, indent=2)
        handle.write("\n")
    print(f"\n→ {OUT_NAME} écrit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
