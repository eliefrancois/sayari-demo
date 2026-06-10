# 10 - Manual Test Plan

A by-hand smoke suite for the live demo. Run it against the current Cloud Run
revision (latest as of writing: `sayari-demo-backend-00031-sj9`). Episodic vector
(Phase D) is flag-off, so nothing here exercises it; everything runs on the live
default config.

For each step, note: did recall work, did the tool feed show `recall_state` when
expected (instead of re-running a tool), were there any "Other"/unnamed graph
nodes, and any count/accuracy nits (e.g. an asserted total that does not match the
named list).

---

## Test 1 - Rosneft multi-turn memory (recall + recap routing + no truncation)

- [ ] `Investigate Rosneft`
  - Expect: full RiskSummary card, sanctioned subsidiaries named, graph has no
    "Other"/unresolved nodes.
- [ ] `Which subsidiaries were sanctioned again?`
  - Expect: tool feed shows `recall_state` (not a re-run of `check_sanctions`);
    the list comes back by name, including Rosneft Trading S.A.
- [ ] `Summarize everything you found on Rosneft so far`
  - Expect: a conversational recap (TurnAnswer), NOT a regenerated RiskSummary
    card, ending with an offer to generate the formal report. Zero
    "(validation failed... retrying)" flashes.

## Test 2 - Explicit formal report (truncation hardening)

Same conversation as Test 1.

- [ ] `Generate a full risk report on Rosneft`
  - Expect: the formal RiskSummary card renders cleanly with no retry loop, even
    though it is large (the 8192 output ceiling covers it).

## Test 3 - Gazprom "most sanctioned" ranking (Phase B)

- [ ] `Investigate Gazprom`
- [ ] `Profile the most sanctioned entity connected to it`
  - Expect: ranks across the full registry (OFAC SDN entities like Gazprom
    Shelfproekt in play, not just Belgazprombank by factor count), states its
    ranking criterion, and offers to re-sort.
- [ ] `Sort by number of sanctions regimes instead`
  - Expect: re-ranks on the alternate criterion without re-investigating (uses
    `recall_state`).

## Test 4 - Provenance re-cite (Phase E)

Same conversation as Test 3.

- [ ] `What's the source for the Achinsk Refinery sanctions claim?`
      (or any entity it named earlier)
  - Expect: it cites the source (OpenSanctions record / Sayari id / risk factor)
    from `recall_state` provenance without re-running a search or profile tool.

## Test 5 - Graph resolver quality (Phase 0)

- [ ] `Investigate Gazprom` (fresh conversation)
  - Expect: risk-traversal nodes are named, no anonymous "Other: ...JcvPXQ"
    nodes, multi-hop path nodes resolved.

## Test 6 - Leads toggle + search relevance

- [ ] `Search for Sberbank`
  - Expect: agent narrates pinned vs total candidates; the leads overlay toggle
    shows/hides the full lead set; pinned nodes are relevant to the query.

## Test 7 - Sanctions labeling accuracy

- [ ] `Investigate Rostec`
  - Expect: OFAC SDN vs non-SDN labeled correctly; PEP flagged distinctly; no
    dismissed name-collision shown as a confirmed hit.

## Test 8 - Injection stays flat (Phase C, observational)

Run one long conversation, 5+ turns across different sub-entities (investigate,
ask about subsidiaries, profile one, ask about its officers, recap).

- [ ] Late-turn answers stay as sharp as early ones; no degradation as the
      conversation grows.

---

## Known watch-items

- Count accuracy: in a prior run the agent said Sechin was "sanctioned across 10
  jurisdictions" but listed 9, and asserted "13 sanctioned entities" while naming
  only ~5. Watch whether asserted totals match the named list.
- Two live evals are known non-deterministic flakes (`ofac_non_sdn_labeling`,
  `used_sayari_record`); they are model tool-choice/phrasing, not regressions.
