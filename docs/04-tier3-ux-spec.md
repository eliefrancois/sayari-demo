# Tier 3 UX Spec: Branching Investigation Canvas

Design reference for the Tier 3 frontend. Inspiration: Max's `local-lmcanvas-web` branching chat canvas. Do **not** start building this until Tier 1 is verified end-to-end (it touches `GraphPanel`, `schema`, `types`, which Tier 1 also edits).

Mockup: `~/.cursor/projects/Users-efrancois-Desktop-Projects-unicover/assets/tier3-ux-mockup.png`

---

## 0. Confirmed scope (locked 2026-06-08)

These decisions are final and override anything looser below.

- **Goal:** make the whole app look and feel like lmcanvas. The current sayari UI is being replaced, not augmented.
- **Scope:** FULL-APP reskin. Every surface (entry, investigation UI, all panels) adopts the lmcanvas design language.
- **Component reuse:** lift as many lmcanvas components as possible (card, `OffsetEdge`, tool-call blocks, grid background, fork hooks, motion) and adapt only at the seam where they meet sayari's SSE stream and conversation store. Code donor is the real app at `/Users/efrancois/Desktop/Projects/local-lmcanvas` (MIT licensed, React 19 + `@xyflow/react`, same canvas lib we use). The marketing mock at `local-lmcanvas-web` is visual reference only.
- **Theme:** light default, Geist fonts (Variable / Mono / Pixel), OKLCH token set. Token-driven so a dark variant is a later swap.
- **Color:** functional accents only. The chrome stays neutral lmcanvas grayscale; the ONLY color is risk severity (glow / fill) and source (ring / dot) per section 2.
- **Layout:** the branching canvas is the PRIMARY UI. Tool calls render INSIDE the branch cards (lmcanvas pattern), so the separate Tool Feed panel is removed. Split-pane stays: Investigation tree (left) and Evidence graph (right, existing React Flow restyled).
- **Sequencing:** start only after the Tier 2 build lands. Both touch `GraphPanel`, `types`, and `schema`, so running them at once would collide.

---

## 1. Design language (borrowed from lmcanvas)

- **Type:** Geist Variable (sans) for body, Geist Mono for all metadata/labels (uppercase, tracked, 8-11px), optional Geist Pixel Square for the model/route chip accent only.
- **Palette (neutral chrome):** OKLCH grayscale. `--background: oklch(0.985 0 0)`, `--foreground: oklch(0.145 0 0)`, `--card: oklch(1 0 0)`, `--muted: oklch(0.97 0 0)`, `--border: oklch(0.922 0 0)`, `--grid-line: oklch(0.94 0 0)`. Light default; build with tokens so a dark variant is a token swap.
- **Canvas:** fixed 32px grid (`linear-gradient` lines) + radial vignette fading the grid into the background at edges.
- **Cards:** white, 1px hairline border, `rounded-[10px]`, `shadow-sm`, lift-shadow on drag, top padding reserved for a corner badge.
- **Motion (framer-motion):** drop-in scale from top origin, height auto-grow as content streams, pulsing caret, edge paths morph with `[0.16, 1, 0.3, 1]` easing. Durations 0.12-0.28s.

## 2. Color carries meaning (our divergence from lmcanvas)

lmcanvas is intentionally colorless. We are a risk product, so the chrome stays neutral and **color is reserved for two independent signals**:

- **Risk severity (glow / fill):** `critical` = red, `high` = orange, `elevated` = amber, `relevant` = gray. Maps to Sayari risk-factor `level`.
- **Source (ring / dot):** Sayari = indigo/blue, ICIJ = magenta/violet, OpenSanctions = teal. Keep these visually distinct (the mockup had Sayari/ICIJ too similar; separate hue families).

**Layering rule:** `ring = source`, `glow/fill = top risk level`. A sanctioned Sayari entity renders as a blue ring + red glow, so provenance is never lost on the scariest nodes. The legend shows both scales.

## 3. Layout

Split-pane, thin vertical hairline divider:
- **Left (~40%), label `INVESTIGATION`:** the branch tree.
- **Right (~60%), label `EVIDENCE GRAPH`:** the React Flow graph + bottom-left legend card.
- Thin top header: product name, route/model chip (pixel font), `Reset`.

## 4. Investigation tree (left)

- **Branch cards** = chat turns. Corner badge shows **thread type** (Ownership / Sanctions / Trade / Identity), color-coded (replaces lmcanvas's model picker).
- Card body: user question (bold) → divider → streaming assistant text → collapsible tool-call blocks.
- **Tool-call blocks:** reuse lmcanvas `DemoToolCall` pattern (or prompt-kit `Tool`/`Source`): `rounded-[8px]` bordered row, icon + name + mono summary + status (spinner / check / failed), expand to mono `input`/`result` on muted bg.
- **Fork:** hover reveals a circular `+` at the card edge; analyst creates branches (user-driven). Agent drives graph + suggested follow-ups, not branch creation.
- **Layout:** auto-laid-out tidy tree on create, draggable/nudgeable after. Edges = bezier connectors (muted-foreground stroke, ~2.25 width, rounded cap), morph on drag.
- **Default is conversational; the report is on-demand.** Normal turns end with findings + suggested follow-up chips (and clarifying questions if the query is vague). The agent does NOT auto-generate the formal report. When it has enough (resolved entity + ≥1 risk/ownership/sanctions signal) it sets a guarded `report_ready` flag, which surfaces a clickable **report-ready badge** on that chat node, plus a soft "Compile risk report" suggestion chip and a one-line inline offer.
- **Risk Report card:** clicking the badge compiles `submit_summary` over the **per-path accumulated evidence** (everything along the active branch up to that node) into a visually distinct "Risk Report" card (not a normal turn): structured claims + risk factors grouped by level, claims click-to-highlight their path in the graph. The card supports **PDF export** via a designed `@react-pdf/renderer` template (header + entity + claims by confidence + risk factors by level + sources/provenance + generated date) so the analyst can hand off a real document. (Fallback if time-constrained: html2canvas + jsPDF DOM capture.)

## 5. Evidence graph (right)

- Restyle existing `GraphPanel` to the design language. Nodes circular, `ring = source`, `glow = risk`.
- Render each Sayari risk-factor `traversal_path` as a highlighted chain (thicker colored edges) on demand.
- Edges: thin curved lines with relationship labels (`owns`, `officer of`, `registered at`).
- **Legend card** (bottom-left): two sections, RISK (4 dots) and SOURCE (3 dots). Doubles as a filter later.

## 6. Time-travel (branch ↔ graph link)

- Click a branch card → graph regenerates to that turn's **accumulated state along the active path** (union of deltas). New-this-turn nodes pulse in; inherited nodes dim. Sibling branches are context-isolated.
- Optional later: a bottom timeline scrubber to step through turns of the active path.

## 7. Build order (when unblocked)

1. ✅ Tokens + grid canvas + card/motion primitives (light theme). *(stage 1, commit 19f3a7a)*
2. ✅ Restyle `GraphPanel`: source rings + risk glow + legend + traversal-path highlight. *(stage 1, commit 19f3a7a)*
3. ✅ Branch tree: cards, badges, tool blocks, fork `+`, auto-layout + drag, bezier edges. *(stage 2b)*
4. ✅ Time-travel: click-to-regen with pulse/dim (needs Tier 3 backend conversation tree + per-turn graph deltas). *(stage 2b, on the stage 2a backend)*
5. Risk Report card + claim click-to-highlight. *(card + highlight landed in stages 1–2b; PDF export still open)*
