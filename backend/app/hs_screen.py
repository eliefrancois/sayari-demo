"""Dual-use HS-code screen: match Sayari shipment HS codes against a curated list.

Sayari ships 6-digit HS codes but no per-code dual-use flag, so we screen them
against a bundled reference (BIS/E5 CHPL, `data/hs_dual_use.json`) and tag hits
as "our HS screen", kept provenance-distinct from Sayari's own native BIS tags.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger("erre.hs_screen")

_ASSET = Path(__file__).parent / "data" / "hs_dual_use.json"


@lru_cache(maxsize=1)
def _reference() -> dict[str, dict[str, Any]]:
    """code (6-digit, digits-only) -> {description, tier, list}. Loaded once and
    cached. Fails OPEN to an empty map (the screen then simply finds nothing) so
    a missing/corrupt asset never crashes a trade investigation."""
    try:
        raw = json.loads(_ASSET.read_text())
    except Exception:  # pragma: no cover - defensive
        log.warning("hs_dual_use asset load failed", exc_info=True)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for entry in raw.get("codes") or []:
        if not isinstance(entry, dict):
            continue
        code = _norm(entry.get("code"))
        if code:
            out[code] = {
                "description": entry.get("description"),
                "tier": entry.get("tier"),
                "list": entry.get("list"),
            }
    return out


def _norm(code: Any) -> str:
    """Normalize an HS code to its 6-digit, digits-only prefix. Sayari codes are
    already 6-digit but may arrive with dots or extra precision; we compare on the
    leading 6 digits so '8542.31' and '854231xx' both match '854231'."""
    digits = re.sub(r"\D", "", str(code or ""))
    return digits[:6] if len(digits) >= 6 else digits


def screen_hs_codes(codes: list[Any]) -> list[dict[str, Any]]:
    """Return the dual-use HITS among `codes` (deduped, order-preserving). Each
    hit is {code, description, tier, list, provenance:"hs_screen"}. Empty list
    means none of the supplied HS codes are on our curated dual-use reference."""
    ref = _reference()
    seen: set[str] = set()
    hits: list[dict[str, Any]] = []
    for c in codes or []:
        norm = _norm(c)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        match = ref.get(norm)
        if match:
            hits.append({
                "code": norm,
                "description": match.get("description"),
                "tier": match.get("tier"),
                "list": match.get("list"),
                "provenance": "hs_screen",
            })
    return hits


# Sayari NATIVE party-risk tag fragments that signal export-control / dual-use
# exposure. These come straight off a trade party's `risks` dict (NOT our HS
# screen), surfaced with provenance "sayari_bis_tag" so the two never blur.
_NATIVE_DUAL_USE_FRAGMENTS = (
    "bis_high_priority",      # exports_bis_high_priority_items_*
    "usa_bis",                # export_controls_bis_entity, owner_of_usa_bis_entity
    "bis_entity",
    "bis_meu",                # controlled_by_bis_meu
    "export_control",
    "export_to_",             # export_to_blr_soe, export_to_rus_*
    "dual_use",
)


def native_bis_tags(risks: Any) -> list[str]:
    """The export-control / dual-use risk-factor NAMES on a Sayari trade party's
    `risks` dict (e.g. `exports_bis_high_priority_items_*`, `owner_of_usa_bis_entity`,
    `controlled_by_bis_meu`). These are Sayari's OWN tags, provenance
    'sayari_bis_tag', complementing our HS screen. Returns [] when the party
    carries no such tag."""
    if not isinstance(risks, dict):
        return []
    out: list[str] = []
    for name in risks:
        low = str(name).lower()
        if any(frag in low for frag in _NATIVE_DUAL_USE_FRAGMENTS):
            out.append(str(name))
    return out
