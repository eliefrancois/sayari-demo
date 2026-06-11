"""OpenSanctions data layer: a thin async client over /match/default.

Each call answers one question: does this name appear on any global sanctions,
PEP, debarment, or watchlist? Errors are logged and return an empty list rather
than fail the investigation, since a missing check is recoverable but a thrown
exception would kill the whole SSE stream.
"""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings
from app.schema import SanctionsHit

log = logging.getLogger("erre.sanctions")

# Where matches typically live. OpenSanctions has dozens of datasets but these
# are the watchlist-style ones our risk_signals care about. Used to derive the
# `lists` field shown in the UI.
_WATCHLIST_DATASETS = {
    "sanctions",
    "us_ofac_sdn",
    "us_ofac_cons",
    "us_sam_exclusions",
    "us_trade_csl",
    "eu_fsf",
    "eu_meps",
    "gb_hmt_sanctions",
    "ua_nsdc_sanctions",
    "ca_dfatd_sema_sanctions",
    "ch_seco_sanctions",
    "au_dfat_sanctions",
    "jp_mof_sanctions",
    "un_sc_sanctions",
    "interpol_red_notices",
    "mc_fund_freezes",
}

# Below this score, OpenSanctions itself considers the match "weak", so surface
# in the response but tag as low-confidence so the agent can decide.
_STRONG_MATCH_SCORE = 0.70

# Human-readable, UNAMBIGUOUS labels for the datasets we surface. The model only
# ever sees these labels (never the bare slug), so it cannot blur, e.g.,
# `us_ofac_cons` into "OFAC SDN". The single most important distinction here is
# OFAC SDN (blocked persons) vs OFAC Consolidated (non-SDN): conflating them is
# a serious analyst error. Export-control / trade-screening lists are labeled as
# such so they're never mistaken for OFAC blocking sanctions.
_DATASET_LABELS = {
    "us_ofac_sdn": "OFAC SDN List (Specially Designated Nationals — blocked persons)",
    "us_ofac_cons": "OFAC Consolidated List (non-SDN, e.g. SSI/sectoral — NOT blocked)",
    "us_sam_exclusions": "US SAM Exclusions (federal debarment — NOT OFAC)",
    "us_trade_csl": "US Trade CSL (Consolidated Screening List — trade/export screening, incl. BIS Entity List)",
    "us_bis_denied": "BIS Denied Persons List (US export controls — NOT OFAC)",
    "us_bis_entities": "BIS Entity List (US export controls — NOT OFAC)",
    "eu_fsf": "EU Financial Sanctions File (FSF)",
    "eu_meps": "EU Members of Parliament (PEP context — NOT a sanction)",
    "gb_hmt_sanctions": "UK HMT Sanctions",
    "ua_nsdc_sanctions": "Ukraine NSDC Sanctions",
    "ca_dfatd_sema_sanctions": "Canada SEMA Sanctions",
    "ch_seco_sanctions": "Switzerland SECO Sanctions",
    "au_dfat_sanctions": "Australia DFAT Sanctions",
    "jp_mof_sanctions": "Japan MOF/METI Sanctions",
    "un_sc_sanctions": "UN Security Council Consolidated Sanctions",
    "interpol_red_notices": "INTERPOL Red Notices",
    "mc_fund_freezes": "Monaco Asset Freezes",
}


def _classify_datasets(datasets: list[str]) -> tuple[list[str], bool]:
    """Return (human_readable_list_names, is_actual_watchlist_hit).

    The PEP / wikidata / company-registry hits are interesting but NOT the
    same as "this person is sanctioned". Separate so the agent can phrase
    its findings honestly.

    `lists` is mapped to explicit, unambiguous program labels (see
    `_DATASET_LABELS`) precisely so the model reports the program VERBATIM and
    cannot upgrade a non-SDN/Consolidated/Entity-List hit to "OFAC SDN".
    """
    on_watchlist = any(d in _WATCHLIST_DATASETS for d in datasets)
    chosen = [d for d in datasets if d in _WATCHLIST_DATASETS] or datasets[:3]
    pretty = [_DATASET_LABELS.get(d, d) for d in chosen]
    return pretty, on_watchlist


async def check_sanctions(name: str, schema: str = "Person") -> list[SanctionsHit]:
    """Look up `name` against OpenSanctions /match/default.

    Args:
        name: Subject's full name as known.
        schema: OpenSanctions entity schema. "Person" for individuals,
                "Organization" or "Company" for entities. Default Person
                because most agent calls are on Officers.

    Returns:
        List of SanctionsHit, ordered by score desc. Empty list if no
        matches or on network error (logged, not raised).
    """
    s = get_settings()
    url = f"{s.opensanctions_api_url}/match/default"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"ApiKey {s.opensanctions_api_key}",
    }
    payload = {"queries": {"q1": {"schema": schema, "properties": {"name": [name]}}}}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            body = r.json()
    except httpx.HTTPError as e:
        log.warning("sanctions_lookup_failed", extra={"name": name, "error": str(e)})
        return []

    results = body.get("responses", {}).get("q1", {}).get("results", [])
    hits: list[SanctionsHit] = []
    for m in results:
        datasets = m.get("datasets", [])
        lists, is_watchlist = _classify_datasets(datasets)
        props = m.get("properties", {}) or {}
        # OpenSanctions returns list-valued properties even for single values.
        # We pass them through verbatim so the agent sees the same shape it
        # would see in the OS UI, and can spot list mismatches like
        # subject country=US but match countries=["ru", "lt"].
        hits.append(
            SanctionsHit(
                name_searched=name,
                matched_name=m.get("caption", name),
                lists=lists,
                sanctions_id=m.get("id", ""),
                score=float(m.get("score", 0.0)),
                on_watchlist=is_watchlist,
                reason=("topics: " + ", ".join(m.get("topics", []))) if m.get("topics") else None,
                position=props.get("position") or None,
                address=props.get("address") or None,
                countries=props.get("country") or None,
                birth_date=props.get("birthDate") or None,
            )
        )
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


def is_strong_match(hit: SanctionsHit) -> bool:
    """A confident sanctions hit requires BOTH a high name-similarity score AND
    membership on an actual watchlist dataset. Score alone is not enough: a
    wikidata biographical entry or FINRA action can score 1.0 by name without
    being a sanction. Without the watchlist gate, querying "Jeffrey Epstein"
    flags his wikidata page as a 'strong sanctions match', which is wrong."""
    return hit.score >= _STRONG_MATCH_SCORE and hit.on_watchlist
