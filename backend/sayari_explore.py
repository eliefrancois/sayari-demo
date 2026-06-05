"""Scratch harness to explore the Sayari data structure against list_1 entities.

Not wired into the app. Goal: learn the real shape of resolution + entity so we
can design the data layer, tools, and the ICIJ/Sayari overlap story.

Run:  .venv/bin/python sayari_explore.py
Needs: SAYARI_CLIENT_ID / SAYARI_CLIENT_SECRET in backend/.env
"""

from __future__ import annotations

import json
import os

from dotenv import load_dotenv

load_dotenv()

from sayari.client import Sayari  # noqa: E402

CLIENT_ID = os.getenv("SAYARI_CLIENT_ID") or os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("SAYARI_CLIENT_SECRET") or os.getenv("CLIENT_SECRET")

SAMPLES = [
    {"name": "Sberbank", "address": "19 Vavilova St. Moscow", "country": "RUS"},
    {"name": "Gazprom", "address": "16 Nametkina St. Moscow", "country": "RUS"},
    {"name": "Rostec", "address": "24 Usacheva St. Moscow", "country": "RUS"},
    {"name": "Huawei Technologies Co. Ltd.", "address": "Bantian Longgang District Shenzhen", "country": "CHN"},
]


def as_dict(obj):
    for attr in ("dict", "model_dump"):
        if hasattr(obj, attr):
            try:
                return getattr(obj, attr)()
            except Exception:
                pass
    return obj


def trunc(v, n=160):
    s = json.dumps(v, default=str, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


def main() -> None:
    if not CLIENT_ID or not CLIENT_SECRET:
        raise SystemExit("Missing SAYARI_CLIENT_ID / SAYARI_CLIENT_SECRET in backend/.env")

    client = Sayari(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)

    deep_dumped = False
    for s in SAMPLES:
        print("\n" + "=" * 80)
        print(f"{s['name']}  |  {s['address']}  |  {s['country']}")
        res = client.resolution.resolution(
            name=s["name"], address=s["address"], country=s["country"]
        )
        if not res.data:
            print("  (no matches)")
            continue
        best = res.data[0]
        ent = as_dict(client.entity.get_entity(best.entity_id))
        if not isinstance(ent, dict):
            print("  (entity not a dict)")
            continue

        print(f"  resolved: {ent.get('label')}  [{ent.get('type')}]  id={ent.get('id')}")
        print(f"  sanctioned={ent.get('sanctioned')}  pep={ent.get('pep')}  "
              f"psa_count={ent.get('psa_count')}  degree={ent.get('degree')}")
        print(f"  countries={ent.get('countries')}")

        # Risk factor catalog (the headline data): name -> level
        risk = ent.get("risk") or {}
        print(f"  RISK FACTORS ({len(risk)}):")
        for name, data in risk.items():
            d = data if isinstance(data, dict) else {}
            level = d.get("level")
            val = d.get("value")
            meta = d.get("metadata") or {}
            path = meta.get("traversal_path")
            extra = f"  path={trunc(path, 90)}" if path else ""
            print(f"    [{level}] {name} = {trunc(val, 40)}{extra}")

        # Identifier types present (hard match keys)
        id_types = sorted({i.get("type") for i in (ent.get("identifiers") or [])})
        print(f"  IDENTIFIER TYPES: {id_types}")

        # Source provenance
        srcs = ent.get("source_count") or {}
        labels = [v.get("label") for v in srcs.values() if isinstance(v, dict)][:6]
        print(f"  SOURCES ({len(srcs)}): {labels}")

        # One deep dump so we see nested attribute/relationship shape exactly once
        if not deep_dumped:
            deep_dumped = True
            print("\n  --- DEEP: attributes object keys ---")
            attrs = ent.get("attributes") or {}
            if isinstance(attrs, dict):
                for k, v in attrs.items():
                    print(f"    {k}: {trunc(v, 220)}")
            print("\n  --- DEEP: relationships object (top-level keys) ---")
            rels = ent.get("relationships") or {}
            if isinstance(rels, dict):
                print(f"    keys: {sorted(rels.keys())}")
                data = rels.get("data")
                if isinstance(data, list) and data:
                    print(f"    sample relationship[0]: {trunc(data[0], 500)}")


if __name__ == "__main__":
    main()
