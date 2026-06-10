"""Shared agent helpers used by BOTH the native loop and the LangGraph impl.

Extracted from agent_native.py so the two implementations stay byte-for-byte
identical on the parts that have nothing to do with control flow: the model
id, the compressed-context builder, the per-turn digests that feed episodic
memory, the one-line tool-result summaries, and the sanctions-review diff.

Keeping these here means a behavior fix (e.g. a context-block tweak) lands in
native and graph at once — no drift between the two code paths.
"""

from __future__ import annotations

from typing import Any

from app.schema import RiskSummary, TurnAnswer

# --- Sayari risk slimming ---------------------------------------------------
# The single most important guard against the 429s we already fought: NEVER
# send the raw Sayari `risk` map to the model. Gazprom carries 95 factors, each
# with a traversal_path. We compress to: counts-by-level + the direct
# sanctioned/state-owned/export factors verbatim + the top-N ownership-derived
# factors WITH their paths, tagging psa_* (ER-derived) as lower-confidence.

# Severity ordering for "level". Lower rank = more severe = surfaced first.
_LEVEL_RANK = {"critical": 0, "high": 1, "elevated": 2, "relevant": 3}

# Direct/categorical factors we always surface verbatim (the headline hits).
_DIRECT_RISK_NAMES = {"state_owned", "export_controls", "usa_bis", "pep"}


def _level_rank(level: str | None) -> int:
    return _LEVEL_RANK.get((level or "").lower(), 99)


def _is_direct_factor(name: str) -> bool:
    """True for direct/categorical risk factors (the headline hits) — directly
    sanctioned, state-owned, export-controlled — as opposed to ownership-derived
    ones that come with a traversal path."""
    if name in _DIRECT_RISK_NAMES:
        return True
    if name.startswith("sanctioned"):
        return True
    if "export_control" in name:
        return True
    return False


# Sayari ships an OFAC record number under a type literally named
# `usa_ofac_sdn_number`, but OFAC assigns these numbers to BOTH the SDN
# (blocked) list AND the Consolidated (non-SDN) list — so the field NAME does
# not prove SDN listing. Huawei, for example, carries usa_ofac_sdn_number=30947
# yet is on the OFAC Consolidated non-SDN list, not the SDN blocked-persons
# list. We relabel it to a neutral, accurate type before it reaches the model so
# OFAC list membership is determined from the `sanctioned_usa_ofac_*` risk
# factors and check_sanctions `lists` — never inferred from this identifier's
# name. The value is preserved verbatim for provenance.
_MISLEADING_IDENTIFIER_TYPES = {
    "usa_ofac_sdn_number": (
        "usa_ofac_record_number",
        "USA OFAC record number (NOT proof of SDN listing — see sanctions factors)",
    ),
}


def relabel_identifiers(identifiers: list[Any]) -> list[Any]:
    """Rename identifier types whose NAME implies a list membership the value
    does not actually prove (see `_MISLEADING_IDENTIFIER_TYPES`). Pass-through
    for everything else; values are never altered."""
    out: list[Any] = []
    for i in identifiers:
        if not isinstance(i, dict):
            out.append(i)
            continue
        mapped = _MISLEADING_IDENTIFIER_TYPES.get(i.get("type"))
        if mapped:
            j = dict(i)
            j["type"] = mapped[0]
            if "label" in j:
                j["label"] = mapped[1]
            out.append(j)
        else:
            out.append(i)
    return out


def slim_sayari_profile(entity: dict[str, Any], *, top_n_derived: int = 8) -> dict[str, Any]:
    """Compress a raw Sayari EntityDetails dict into a token-budget-safe shape
    for the model.

    Returns identity + flags + a SLIMMED risk block:
      - counts_by_level: how many factors at each severity
      - total_factors: the full count (so the agent knows what it's NOT seeing)
      - direct_factors: sanctioned*/state_owned/export_controls verbatim
      - derived_factors: top-N ownership-derived factors WITH traversal_path,
        psa_* tagged lower-confidence

    This is mandatory before any Sayari risk reaches the LLM.
    """
    if not isinstance(entity, dict):
        return {}
    risk = entity.get("risk") or {}
    if not isinstance(risk, dict):
        risk = {}

    counts_by_level: dict[str, int] = {}
    direct: list[dict[str, Any]] = []
    derived: list[dict[str, Any]] = []

    for name, data in risk.items():
        d = data if isinstance(data, dict) else {}
        level = d.get("level")
        value = d.get("value")
        meta = d.get("metadata") or {}
        path = meta.get("traversal_path") or []
        if isinstance(path, str):
            path = [path]
        counts_by_level[level or "unknown"] = counts_by_level.get(level or "unknown", 0) + 1

        if _is_direct_factor(name):
            direct.append({"name": name, "level": level, "value": value})
        if path:
            # Numeric value == hops; use it as a tiebreak so closer (fewer-hop)
            # exposure ranks above distant exposure within the same level.
            hops = value if isinstance(value, (int, float)) else len(path)
            derived.append(
                {
                    "name": name,
                    "level": level,
                    "value": value,
                    "path": list(path),
                    "psa": name.startswith("psa_"),
                    "_hops": hops,
                }
            )

    # Most severe first; within a level, fewer hops first; non-psa before psa.
    derived.sort(key=lambda f: (_level_rank(f["level"]), f["psa"], f["_hops"]))
    for f in derived:
        f.pop("_hops", None)
    derived = derived[:top_n_derived]

    direct.sort(key=lambda f: _level_rank(f["level"]))

    identifiers = relabel_identifiers([
        {"type": i.get("type"), "value": i.get("value")}
        for i in (entity.get("identifiers") or [])
        if isinstance(i, dict)
    ])[:12]

    return {
        "id": entity.get("id"),
        "label": entity.get("label"),
        "translated_label": entity.get("translated_label"),
        "type": entity.get("type"),
        # A source record id (Sayari `reference_id`) the agent can hand to
        # sayari_record for document-level provenance. None when the entity has no
        # single canonical record reference.
        "record_id": entity.get("reference_id"),
        "sanctioned": entity.get("sanctioned"),
        "pep": entity.get("pep"),
        "psa_count": entity.get("psa_count"),
        "degree": entity.get("degree"),
        "closed": entity.get("closed"),
        "countries": entity.get("countries"),
        "identifiers": identifiers,
        "relationship_count": entity.get("relationship_count") or {},
        "risk": {
            "total_factors": len(risk),
            "counts_by_level": counts_by_level,
            "direct_factors": direct,
            "derived_factors": derived,
            "note": (
                "psa_* factors are ER-derived (Possibly-Same-As) and lower "
                "confidence. Numeric value == hops in the ownership chain."
            ),
        },
    }

def slim_sayari_record(record: dict[str, Any]) -> dict[str, Any]:
    """Compress a raw Sayari record into the provenance-relevant fields the model
    needs: identity + dates + the document/source URLs. The raw record carries
    large nested `references` lists (every entity that cites it) we drop here."""
    if not isinstance(record, dict):
        return {}
    docs = record.get("document_urls") or []
    if not isinstance(docs, list):
        docs = [docs]
    return {
        "id": record.get("id"),
        "label": record.get("label"),
        "source": record.get("source"),
        "source_url": record.get("source_url"),
        "document_urls": docs[:5],
        "publication_date": record.get("publication_date"),
        "acquisition_date": record.get("acquisition_date"),
        "record_url": record.get("record_url"),
        "references_count": record.get("references_count"),
    }


# --- Model + loop constants (shared) --------------------------------------

MODEL = "claude-sonnet-4-5-20250929"  # dated snapshot = reproducible demo behavior
MAX_ITERATIONS = 20  # safety bail-out; real investigations finish in 6-12
# Output ceiling per model call. A legit large RiskSummary (many claims, each with
# source_refs + sanctions_hits + risk-factor paths) can exceed 4096 and get cut off
# mid tool-call JSON, which then fails validation and burns retries. 8192 gives the
# formal report room to land in one shot. Sonnet 4.5 supports it and you only pay
# for tokens actually generated, so the conversational default turn costs no more.
MAX_TOKENS_PER_TURN = 8192

# Per-turn tool-call budget (soft cap). "Answer any question" must not let a
# single turn explode into dozens of tool calls. When the agent crosses this, we
# nudge it (via tool_result text) to wrap up with a terminator; we do NOT hard-
# kill it (MAX_ITERATIONS is the hard stop). A normal investigation is 4-12 calls.
MAX_TOOL_CALLS_PER_TURN = 14


def budget_nudge(tool_calls_so_far: int) -> str | None:
    """A graceful 'wrap up now' message once the per-turn tool budget is hit.
    Returned to the model as extra tool_result context; None when under budget."""
    if tool_calls_so_far < MAX_TOOL_CALLS_PER_TURN:
        return None
    return (
        f"[TOOL BUDGET REACHED: {tool_calls_so_far} tool calls this turn — the "
        f"soft cap is {MAX_TOOL_CALLS_PER_TURN}.] Stop gathering and FINISH NOW: "
        "call submit_answer (default) with what you have, or submit_summary only "
        "if a formal report was explicitly requested. Do not call more "
        "investigation tools."
    )


# --- Compressed episodic context ------------------------------------------

# Phase C (doc 09 §6) fixed-budget caps for the injected INVESTIGATION STATE
# core. These are HARD caps that do NOT scale with investigation size: the core
# is navigation hints (what exists + where to look), never the data itself. The
# agent pages exact/complete rows in with recall_state. Bumping a cap is the one
# knob that grows the core, so keep them tiny and deliberate.
_STATE_PRIMARY_CAP = 2  # primary subject(s) under active investigation
_STATE_PINNED_CAP = 8  # pinned node ids surfaced inline
_STATE_LEADSETS_CAP = 3  # one header line per recent search
_STATE_SANCTIONS_CAP = 5  # confirmed sanctions named inline
# Fallback graph roster (native loop only — no state_doc). The registry pointer
# replaces it on the graph path; this just bounds the legacy path.
_ROSTER_FALLBACK_CAP = 8


def _is_sdn(lists: Any) -> bool:
    """True if any list label is the OFAC SDN (blocked) list — NOT the OFAC
    Consolidated/non-SDN list (same name-collision discipline the prompt and the
    registry enforce). Local so agent_common stays dependency-light."""
    for x in lists or []:
        l = str(x or "").lower()
        if "sdn" in l and "non-sdn" not in l and "non sdn" not in l and "consolidated" not in l:
            return True
    return False


def _render_state_block(state_doc: dict[str, Any] | None) -> list[str]:
    """The small, FIXED-BUDGET `INVESTIGATION STATE` core injected ahead of the
    prose digest (the Letta minimal-inline + retrieve-on-demand split, doc 09
    §6.1). Phase C shrank this from a row dump to pure NAVIGATION HINTS whose
    size does NOT grow with the investigation:

      - the primary subject(s) under investigation (label=id),
      - pinned node ids (a handful),
      - one header line per recent search (count + query),
      - the top few CONFIRMED sanctions BY NAME (high-value, accurate), and
      - a registry pointer (how many entities are tracked + how to rank/enumerate).

    It deliberately does NOT inject the full entity table or full lead lists —
    the agent calls `recall_state` for exact, complete rows on demand. Empty list
    when there's nothing structured yet (e.g. turn 1 or eval mode)."""
    if not state_doc:
        return []
    # Phase B: the unified registry is the entity source of truth (it folds
    # resolved subjects, leads, cached names, AND sanctions hits into one
    # id-keyed pool). Phase C reads it only for the headline subject(s) + counts.
    entities = state_doc.get("entities") or {}
    leads = state_doc.get("leads") or []
    sanctions_adj = state_doc.get("sanctions_adjudicated") or []
    pinned = state_doc.get("pinned_node_ids") or []
    resolved = state_doc.get("resolved_entities") or {}
    if not (entities or leads or sanctions_adj or pinned or resolved):
        return []

    lines: list[str] = [
        "INVESTIGATION STATE (navigation hints only — reuse these IDs, do NOT "
        "re-search; call recall_state for exact/complete rows):"
    ]

    # Primary subject(s): the traversed/profiled subjects (resolved_entities),
    # newest first, capped tiny. NOT raw leads or sanctions-only entities — this
    # names the current focus; the registry pointer below covers the full pool.
    subj_recs = sorted(
        (r for r in resolved.values() if isinstance(r, dict) and r.get("entity_id")),
        key=lambda r: r.get("last_seen_turn") or 0,
        reverse=True,
    )
    if subj_recs:
        parts: list[str] = []
        for r in subj_recs[:_STATE_PRIMARY_CAP]:
            flags = []
            if r.get("sanctioned"):
                flags.append("SANCTIONED")
            if r.get("pep"):
                flags.append("PEP")
            suffix = f" ({', '.join(flags)})" if flags else ""
            parts.append(f"{r.get('label')}={r.get('entity_id')}{suffix}")
        lines.append("Primary subject(s): " + "; ".join(parts))

    if pinned:
        lines.append("Pinned node ids: " + ", ".join(str(p) for p in pinned[:_STATE_PINNED_CAP]))

    # Lead-set headers: one line per recent search, true count + recall pointer.
    if leads:
        sets: dict[Any, dict[str, Any]] = {}
        for l in leads:
            if not isinstance(l, dict):
                continue
            ft = l.get("from_turn")
            s = sets.setdefault(ft, {"count": 0, "query": l.get("from_query")})
            s["count"] += 1
        for ft in sorted((k for k in sets if k is not None), reverse=True)[:_STATE_LEADSETS_CAP]:
            s = sets[ft]
            q = f' ("{s["query"]}")' if s.get("query") else ""
            lines.append(
                f'Leads: {s["count"]} from turn {ft}{q} — '
                f'call recall_state(kind="leads", from_turn={ft}) to enumerate.'
            )

    # Confirmed sanctions BY NAME (high-value, accurate): only `confirmed`
    # verdicts go inline so the core can't misrepresent a dismissed name
    # collision as a hit. Dismissed rows stay recoverable via recall_state.
    confirmed = [
        r for r in sanctions_adj
        if isinstance(r, dict) and r.get("verdict") == "confirmed"
    ]
    if confirmed:
        named: list[str] = []
        for r in confirmed[:_STATE_SANCTIONS_CAP]:
            nm = r.get("matched_name") or r.get("sanctions_id")
            lists = r.get("lists") or []
            if _is_sdn(lists):
                tag = " [OFAC SDN]"
            elif lists:
                tag = f" [{lists[0]}]"
            else:
                tag = ""
            named.append(f"{nm}{tag}")
        line = "Confirmed sanctions: " + "; ".join(named)
        if len(confirmed) > _STATE_SANCTIONS_CAP:
            line += f"; +{len(confirmed) - _STATE_SANCTIONS_CAP} more"
        lines.append(line + ' (recall_state kind="sanctions" for all verdicts).')

    # Registry pointer (replaces the old inline entity dump): the total tracked +
    # how to rank/enumerate. This is the fixed-size handle to the full pool.
    if entities:
        n_sanc = sum(
            1 for r in entities.values()
            if isinstance(r, dict) and r.get("sanctioned")
        )
        sanc_note = f", {n_sanc} sanctioned" if n_sanc else ""
        lines.append(
            f"Registry: {len(entities)} connected entities tracked{sanc_note}. "
            'recall_state(kind="entities", sort="severity") to rank/enumerate; '
            'kind="claims" for prior claims.'
        )

    return lines


def bound_context_digest(text: str, keep_last: int = 15) -> str:
    """Bound the running prose digest so it can't grow without limit: keep the
    last `keep_last` turn-digest lines verbatim and roll older ones into a
    single summary line. Pure string handling — no LLM call. The exact IDs and
    verdicts those older lines referenced already live in `state_doc` (exact,
    via recall_state), so the roll-up loses only narration, never provenance."""
    if not text:
        return text
    lines = text.split("\n")
    if len(lines) <= keep_last:
        return text
    older = lines[:-keep_last]
    recent = lines[-keep_last:]
    roll = (
        f"(earlier in this investigation: {len(older)} prior turn-digest line(s) "
        "rolled up — exact IDs/verdicts preserved in INVESTIGATION STATE / recall_state)"
    )
    return "\n".join([roll] + recent)


def build_context_block(
    context: str,
    graph: dict[str, list],
    pinned_node_ids: list[str],
    force_risk_report: bool,
    state_doc: dict[str, Any] | None = None,
) -> str:
    """Compressed episodic memory injected ahead of the user message.

    Cheap to build (no extra LLM call): the fixed-budget `INVESTIGATION STATE`
    core (primary subject, pinned ids, lead-set headers, confirmed sanctions, and
    a registry pointer — the navigation handle into structured state) on top of a
    running prose digest. `state_doc` is optional so the native loop, which does
    not maintain structured state, keeps its existing behavior unchanged.

    Phase C (doc 09 §6.3): the up-to-30-node "KNOWN GRAPH ENTITIES" roster was a
    capped, truncating UI artifact that leaked into the memory mechanism and
    scaled the injection with case size. When the registry-backed state core is
    present (the graph impl) it already names the subject and points at
    recall_state for the full pool, so the roster is dropped. It survives only as
    a small bounded FALLBACK for the native loop (no state_doc)."""
    state_lines = _render_state_block(state_doc)
    nodes = graph.get("nodes", [])

    if not context and not nodes and not state_lines:
        # Turn 1 — nothing to carry forward.
        return f"force_risk_report: {str(force_risk_report).lower()}\n"

    lines: list[str] = []
    if state_lines:
        lines.extend(state_lines)
        lines.append("")

    lines.append("CONVERSATION CONTEXT (prior turns):")
    lines.append(context.strip() or "(none yet)")

    # Fallback graph roster ONLY when there's no structured state core (native
    # loop). On the graph path the INVESTIGATION STATE core + recall_state cover
    # this, so we keep injection lean and skip it entirely.
    if nodes and not state_lines:
        lines.append("")
        lines.append(
            "KNOWN GRAPH ENTITIES (reuse these node_ids in follow-ups; do NOT "
            "re-run search_entity on a subject already here):"
        )
        for n in nodes[:_ROSTER_FALLBACK_CAP]:
            label = n.get("label") or n.get("type") or "Node"
            lines.append(f"- {n.get('name', '?')} (id={n.get('id')}) [{label}]")
        if len(nodes) > _ROSTER_FALLBACK_CAP:
            lines.append(f"- ...and {len(nodes) - _ROSTER_FALLBACK_CAP} more")

    if pinned_node_ids:
        lines.append("")
        lines.append(
            "PINNED NODES (the user explicitly asked to focus here — prioritize "
            f"them): {', '.join(pinned_node_ids)}"
        )

    lines.append("")
    lines.append(f"force_risk_report: {str(force_risk_report).lower()}")
    return "\n".join(lines) + "\n"


def build_turn_message(context_block: str, user_message: str, turn_index: int) -> str:
    """The single user-role message text for a turn: context + the message."""
    return f"{context_block}\n---\nUSER MESSAGE (turn {turn_index}):\n{user_message}"


# --- Phase 2.5: deterministic follow-up prefetch (doc 09 §6.4) -------------
# Retrieval (a keyword match -> ONE bounded slice), NOT stuffing (everything,
# every turn). When a conversational follow-up clearly asks to enumerate a known
# bucket ("which subsidiaries were sanctioned", "list the leads"), the lean core
# would force a recall_state round-trip; this injects the slice up front so the
# common follow-up answers in one hop. Strictly bounded (a few rows) and only
# fires on a keyword hit, so it can't reintroduce the context-stuffing smell.

_PREFETCH_SANCTIONS_KEYWORDS = ("sanction", "sdn", "watchlist", "blocked", "subsidiar")
_PREFETCH_LEADS_KEYWORDS = ("lead", "search result", "candidate")
_PREFETCH_ROW_CAP = 6


def build_followup_prefetch(
    state_doc: dict[str, Any] | None,
    user_message: str,
) -> str:
    """ONE bounded, retrieval-shaped slice for a follow-up whose keywords match a
    known bucket. Returns "" when nothing matches or there's no state. Pure and
    deterministic — reads only `state_doc`, spends no credits, makes no LLM call.
    Designed to fire only for `conversational_followup` (the caller gates it)."""
    if not state_doc or not user_message:
        return ""
    msg = user_message.lower()

    if any(k in msg for k in _PREFETCH_SANCTIONS_KEYWORDS):
        rows = [r for r in (state_doc.get("sanctions_adjudicated") or []) if isinstance(r, dict)]
        if rows:
            # confirmed first, then dismissed (the Rosneft name-collision rows the
            # agent must be able to re-name without re-running check_sanctions).
            rows.sort(key=lambda r: 0 if r.get("verdict") == "confirmed" else 1)
            out: list[str] = []
            for r in rows[:_PREFETCH_ROW_CAP]:
                nm = r.get("matched_name") or r.get("sanctions_id")
                tag = " [OFAC SDN]" if _is_sdn(r.get("lists")) else (
                    f" [{(r.get('lists') or ['?'])[0]}]"
                )
                out.append(f"- {nm}{tag} — {r.get('verdict')} (sanctions_id={r.get('sanctions_id')})")
            extra = (
                f"\n(+{len(rows) - _PREFETCH_ROW_CAP} more — recall_state kind=\"sanctions\")"
                if len(rows) > _PREFETCH_ROW_CAP else ""
            )
            return (
                "PREFETCHED sanctions ledger (deterministic match on your question — "
                "exact rows, confirmed AND dismissed; recall_state kind=\"sanctions\" "
                "for the rest):\n" + "\n".join(out) + extra
            )

    if any(k in msg for k in _PREFETCH_LEADS_KEYWORDS):
        leads = [l for l in (state_doc.get("leads") or []) if isinstance(l, dict)]
        if leads:
            turns = [l.get("from_turn") for l in leads if l.get("from_turn") is not None]
            recent = max(turns) if turns else None
            recent_set = [l for l in leads if l.get("from_turn") == recent] if recent is not None else leads
            out = []
            for i, l in enumerate(recent_set[:_PREFETCH_ROW_CAP], start=1):
                flags = []
                if l.get("sanctioned"):
                    flags.append("SANCTIONED")
                if l.get("pep"):
                    flags.append("PEP")
                suffix = f" ({', '.join(flags)})" if flags else ""
                out.append(f"- [{i}] {l.get('label')}{suffix} (id={l.get('entity_id')})")
            extra = (
                f"\n(+{len(recent_set) - _PREFETCH_ROW_CAP} more — recall_state kind=\"leads\")"
                if len(recent_set) > _PREFETCH_ROW_CAP else ""
            )
            hdr = f"turn {recent}" if recent is not None else "most recent search"
            return (
                f"PREFETCHED leads from {hdr} (deterministic match on your question — "
                "exact rows; recall_state kind=\"leads\" with from_turn/index for more):\n"
                + "\n".join(out) + extra
            )

    return ""


# --- Per-turn digests (feed the compressed episodic memory) ----------------


def digest_summary(turn_index: int, s: RiskSummary) -> str:
    top_claims = "; ".join(c.text for c in s.claims[:2]) if s.claims else "none"
    signals = ", ".join(s.risk_signals) if s.risk_signals else "none"
    sanc = len(s.sanctions_hits)
    return (
        f"Turn {turn_index} [investigation]: subject={s.entity_name} "
        f"(id={s.entity_id}, found={s.found}). Top claims: {top_claims}. "
        f"Risk signals: {signals}. Confirmed sanctions: {sanc}."
    )


def digest_answer(turn_index: int, user_message: str, a: TurnAnswer) -> str:
    snippet = a.answer.strip().replace("\n", " ")
    if len(snippet) > 220:
        snippet = snippet[:220] + "..."
    if a.clarification_questions:
        return (
            f"Turn {turn_index} [clarify]: user said '{user_message}'. "
            f"Agent asked: {' | '.join(a.clarification_questions)}"
        )
    return f"Turn {turn_index} [follow-up]: Q='{user_message}'. A={snippet}"


# --- Tool-result helpers ---------------------------------------------------


def graph_payload(tool_name: str, parsed: dict[str, Any]) -> tuple[list, list]:
    """Nodes/edges a tool result should contribute to the canvas.

    search_entity returns up to 10 fuzzy name matches that include noise
    (e.g. "Sergey Lazo street" matching "Sergey Roldugin"). We send them to
    the model in the tool_result so it can pick, but we do NOT add them to the
    graph — the graph should only show entities the agent decided to traverse.
    sayari_resolve is the Sayari analogue: it returns ranked candidates the
    agent must pick from, so its results stay off the canvas too.
    recall_state / recall_memory are pure reads over stored memory — they must
    NEVER add nodes.
    """
    if tool_name in ("search_entity", "sayari_resolve", "recall_state", "recall_memory"):
        return [], []
    return parsed.get("nodes", []), parsed.get("edges", [])


# Property keys worth keeping in the slimmed tool result sent to the MODEL.
# Everything the agent actually reasons about (status -> struck_off, jurisdiction,
# country, key dates, company type) stays; verbose/internal fields are dropped.
# The frontend still receives full node properties via the separate graph events,
# so this only shrinks the model's message history.
_MODEL_PROP_KEYS = frozenset({
    "status",
    "struck_off_date",
    "inactivation_date",
    "incorporation_date",
    "dorm_date",
    "jurisdiction",
    "jurisdiction_description",
    "country_codes",
    "countries",
    "company_type",
    "type",
    "service_provider",
})


def slim_result_for_model(parsed: dict[str, Any]) -> dict[str, Any]:
    """Compact copy of a tool result for the model's message history.

    Tool results are re-sent on every agent iteration, so a long investigation
    pays for early results many times over — that quadratic token growth is what
    trips Anthropic's per-minute input-token rate limit. We keep node identity
    (id/name/label/source) plus a few reasoning-relevant properties and drop the
    rest. The UI is unaffected: it renders from the full nodes carried on the
    separate `tool_call_result` graph events, not from this payload.
    """
    if not isinstance(parsed, dict):
        return parsed
    nodes = parsed.get("nodes")
    if not isinstance(nodes, list):
        return parsed
    slim_nodes: list[Any] = []
    for n in nodes:
        if not isinstance(n, dict):
            slim_nodes.append(n)
            continue
        props = n.get("properties") or {}
        kept = {
            k: props[k]
            for k in _MODEL_PROP_KEYS
            if k in props and props[k] not in (None, "", [])
        }
        node = {
            "id": n.get("id"),
            "label": n.get("label"),
            "name": n.get("name"),
            "source": n.get("source"),
        }
        if kept:
            node["properties"] = kept
        slim_nodes.append(node)
    out = dict(parsed)
    out["nodes"] = slim_nodes
    return out


def short_summary(tool_name: str, parsed: dict[str, Any]) -> str:
    """One-line human-readable summary of a tool result for the tool-call feed."""
    if tool_name == "search_entity":
        n = len(parsed.get("nodes", []))
        return f"found {n} match{'es' if n != 1 else ''}"
    if tool_name in {"get_relationships", "get_officers", "find_address_connections", "find_er_links"}:
        n = len(parsed.get("nodes", []))
        e = len(parsed.get("edges", []))
        meta = parsed.get("metadata", {})
        extra = f" (capped at {meta.get('capped_at')})" if meta.get("capped_at") else ""
        return f"{n} nodes, {e} edges{extra}"
    if tool_name == "check_sanctions":
        c = parsed.get("count", 0)
        strong = parsed.get("any_strong_match", False)
        return f"{c} hit{'s' if c != 1 else ''}" + (" — STRONG MATCH" if strong else "")
    if tool_name == "sayari_resolve":
        c = parsed.get("count", 0)
        return f"{c} candidate{'s' if c != 1 else ''}"
    if tool_name == "sayari_profile":
        risk = (parsed.get("profile") or {}).get("risk") or {}
        total = risk.get("total_factors", 0)
        sanc = (parsed.get("profile") or {}).get("sanctioned")
        tag = " — SANCTIONED" if sanc else ""
        return f"{total} risk factor{'s' if total != 1 else ''}{tag}"
    if tool_name == "sayari_ownership":
        n = len(parsed.get("nodes", []))
        e = len(parsed.get("edges", []))
        direction = parsed.get("metadata", {}).get("direction", "")
        return f"{n} nodes, {e} edges ({direction})"
    if tool_name == "sayari_search":
        c = parsed.get("count", 0)
        return f"{c} lead{'s' if c != 1 else ''}"
    if tool_name == "sayari_summary":
        risk = (parsed.get("profile") or {}).get("risk") or {}
        total = risk.get("total_factors", 0)
        sanc = (parsed.get("profile") or {}).get("sanctioned")
        tag = " — SANCTIONED" if sanc else ""
        return f"{total} risk factor{'s' if total != 1 else ''}{tag} (summary)"
    if tool_name == "sayari_watchlist":
        n = len(parsed.get("nodes", []))
        paths = parsed.get("paths", 0)
        return f"{paths} watchlist path{'s' if paths != 1 else ''}, {n} nodes"
    if tool_name == "sayari_trade":
        shown = parsed.get("shown_shipments", 0)
        total = (parsed.get("facets") or {}).get("shipment_count", shown)
        du = parsed.get("dual_use_screen") or {}
        tag = f" — {du.get('shipments_flagged')} DUAL-USE" if du.get("any") else ""
        return f"{shown} of {total} shipments ({parsed.get('role', '')}){tag}"
    if tool_name == "sayari_shortest_path":
        p = parsed.get("path") or {}
        if not p.get("found"):
            return "no path found"
        hops = len(p.get("hops") or [])
        tag = " — SANCTIONED INTERMEDIARY" if p.get("has_sanctioned_intermediary") else ""
        return f"path found: {hops} hop{'s' if hops != 1 else ''}{tag}"
    if tool_name == "sayari_record":
        rec = parsed.get("record") or {}
        docs = len(rec.get("document_urls") or [])
        return f"record: {rec.get('source') or 'source'} ({docs} doc link{'s' if docs != 1 else ''})"
    if tool_name == "recall_state":
        n = parsed.get("count", 0)
        total = parsed.get("total_in_state", 0)
        return f"recalled {n} of {total} from memory"
    if tool_name == "recall_memory":
        if parsed.get("configured") is False:
            return "episodic memory not configured"
        n = parsed.get("count", 0)
        return f"recalled {n} episode{'s' if n != 1 else ''}"
    return "ok"


# --- Sanctions review diff -------------------------------------------------


def build_sanctions_review(
    terminator: RiskSummary | TurnAnswer,
    raw_strong_hits: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Compare raw strong watchlist hits to what the agent kept in the terminator.

    Works for BOTH terminator shapes: a RiskSummary (investigation turn) or a
    TurnAnswer (answer turn) — both carry a `sanctions_hits` list of the hits the
    agent confirmed. Everything else in `raw_strong_hits` is, by construction, a
    strong match the agent dismissed (a name collision). Returns the review
    payload, or None when there were no strong hits to adjudicate (in which case
    callers skip the sanctions_review event)."""
    if not raw_strong_hits:
        return None
    confirmed_ids = {h.sanctions_id for h in terminator.sanctions_hits}
    confirmed = [h for h in raw_strong_hits if h.get("sanctions_id") in confirmed_ids]
    dismissed = [h for h in raw_strong_hits if h.get("sanctions_id") not in confirmed_ids]
    return {
        "raw_strong_count": len(raw_strong_hits),
        "confirmed": confirmed,
        "dismissed": dismissed,
    }
