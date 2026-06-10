/**
 * Risk-report PDF export via the browser's print-to-PDF pipeline.
 *
 * Approach: build a self-contained, print-styled HTML document from the
 * RiskSummary, open it in a new window, and call print(). The user saves it
 * as a PDF from the native dialog. This keeps the export vector-quality and
 * text-selectable (unlike html2canvas rasters), needs zero new dependencies
 * (markdown rendering reuses `marked`, already in the bundle), and the
 * layout is plain CSS we fully control — a clean 1-3 page memo.
 */

import { marked } from "marked";
import type { GraphNode, RiskSummary } from "./types";
import { humanizeRiskFactor, sayariLevelRank } from "./types";

const esc = (s: string): string =>
  s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

/** Render a markdown narrative; agent output only, same trust as the app UI. */
const md = (s: string): string => marked.parse(s, { async: false }) as string;
const mdInline = (s: string): string =>
  marked.parseInline(s, { async: false }) as string;

const RISK_SIGNAL_LABELS: Record<string, string> = {
  sanctioned: "Sanctioned",
  connected_to_sanctioned: "Connected to sanctioned",
  struck_off: "Struck off",
  shell_company_pattern: "Shell-company pattern",
  shared_address_with_many_entities: "Mass-registration address",
  nominee_director_pattern: "Nominee directors",
  cross_leak_presence: "Cross-leak presence",
};

const LEVEL_COLORS: Record<string, string> = {
  critical: "#b91c1c",
  high: "#c2410c",
  elevated: "#b45309",
  relevant: "#525252",
};

function sourceLabel(
  ref: NonNullable<RiskSummary["claims"][number]["source_refs"]>[number],
  nodesById?: Map<string, GraphNode>
): string {
  if (ref.node_id) {
    const node = nodesById?.get(ref.node_id);
    if (node) return `${node.name} (ICIJ graph)`;
    return `ICIJ node …${ref.node_id.slice(-6)}`;
  }
  if (ref.sanctions_id) return `OpenSanctions ${ref.sanctions_id}`;
  if (ref.risk_factor) return `Sayari · ${humanizeRiskFactor(ref.risk_factor)}`;
  if (ref.sayari_entity_id) return `Sayari entity …${ref.sayari_entity_id.slice(-8)}`;
  if (ref.leak) return ref.leak;
  return ref.source;
}

export function buildRiskReportHtml(
  summary: RiskSummary,
  nodesById?: Map<string, GraphNode>
): string {
  const date = new Date().toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });

  const signals = summary.risk_signals
    .map(
      (s) =>
        `<span class="signal">${esc(RISK_SIGNAL_LABELS[s] ?? s.replace(/_/g, " "))}</span>`
    )
    .join("");

  const claims = summary.claims
    .map((c) => {
      const sources = c.source_refs
        .map((r, i) => `<span class="src">[${i + 1}] ${esc(sourceLabel(r, nodesById))}</span>`)
        .join(" ");
      return `<div class="claim">
        <div class="claim-head"><span class="conf conf-${c.confidence}">${c.confidence}</span></div>
        <div class="claim-text">${mdInline(c.text)}</div>
        ${sources ? `<div class="claim-srcs">${sources}</div>` : ""}
      </div>`;
    })
    .join("");

  const sanctions = summary.sanctions_hits
    .map(
      (h) => `<div class="hit">
        <div class="hit-head"><strong>${esc(h.matched_name)}</strong><span class="hit-score">score ${h.score.toFixed(2)}</span></div>
        <div class="hit-lists">${h.lists.map((l) => `<span class="list">${esc(l)}</span>`).join("")}</div>
        ${h.reason ? `<div class="hit-reason">${esc(h.reason)}</div>` : ""}
      </div>`
    )
    .join("");

  const factors = (summary.sayari_risk_factors ?? [])
    .slice()
    .sort((a, b) => sayariLevelRank(a.level) - sayariLevelRank(b.level))
    .map(
      (f) => `<li>
        <span class="level" style="color:${LEVEL_COLORS[f.level] ?? "#525252"}">${esc(f.level)}</span>
        ${esc(humanizeRiskFactor(f.name))}${f.psa ? ' <span class="psa">(ER-derived)</span>' : ""}
      </li>`
    )
    .join("");

  const ownership = (summary.sayari_risk_factors ?? []).filter(
    (f) => (f.path ?? []).length > 0
  );
  const ownershipRows = ownership
    .map((f) => {
      const hops = (f.path ?? []).reduce((acc, p) => {
        const ids = p.split("|").filter((_, i) => i % 2 === 0);
        return Math.max(acc, Math.max(0, ids.length - 1));
      }, 0);
      return `<li>${esc(humanizeRiskFactor(f.name))}${hops ? ` — ${hops} hop${hops === 1 ? "" : "s"} via the ownership/control chain` : ""}</li>`;
    })
    .join("");

  return `<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>Risk Report — ${esc(summary.entity_name)}</title>
<style>
  @page { margin: 22mm 18mm; }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    color: #171717; margin: 0; font-size: 11.5px; line-height: 1.55;
  }
  .kicker {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 9px; letter-spacing: 0.18em; text-transform: uppercase; color: #737373;
  }
  header { border-bottom: 2px solid #171717; padding-bottom: 12px; margin-bottom: 16px; }
  h1 { font-size: 22px; margin: 4px 0 2px; letter-spacing: -0.01em; }
  .meta { color: #737373; font-size: 10.5px; }
  .status {
    display: inline-block; border: 1px solid #d4d4d4; border-radius: 4px;
    padding: 1px 7px; font-family: ui-monospace, Menlo, monospace; font-size: 9px;
    letter-spacing: 0.14em; text-transform: uppercase; margin-left: 8px;
  }
  h2 {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 10px; letter-spacing: 0.18em; text-transform: uppercase;
    color: #525252; border-bottom: 1px solid #e5e5e5; padding-bottom: 4px;
    margin: 20px 0 10px;
  }
  .signals { margin: 10px 0 0; }
  .signal {
    display: inline-block; border: 1px solid #d4d4d4; border-radius: 999px;
    padding: 2px 9px; margin: 0 6px 6px 0; font-size: 10px; font-weight: 600;
  }
  .narrative p { margin: 0 0 8px; }
  .claim { border: 1px solid #e5e5e5; border-radius: 6px; padding: 8px 10px; margin-bottom: 8px; page-break-inside: avoid; }
  .conf {
    font-family: ui-monospace, Menlo, monospace; font-size: 8.5px;
    letter-spacing: 0.12em; text-transform: uppercase; border: 1px solid #d4d4d4;
    border-radius: 3px; padding: 1px 5px;
  }
  .conf-high { background: #f5f5f5; font-weight: 700; }
  .conf-medium { color: #525252; }
  .conf-low { color: #a3a3a3; }
  .claim-text { margin-top: 4px; }
  .claim-srcs { margin-top: 5px; }
  .src { color: #737373; font-size: 9.5px; margin-right: 10px; }
  .hit { border: 1px solid #fecaca; background: #fef2f2; border-radius: 6px; padding: 8px 10px; margin-bottom: 8px; page-break-inside: avoid; }
  .hit-head { display: flex; justify-content: space-between; color: #991b1b; }
  .hit-score { font-family: ui-monospace, Menlo, monospace; font-size: 9.5px; color: #dc2626; }
  .hit-lists { margin-top: 4px; }
  .list {
    display: inline-block; background: #fee2e2; color: #b91c1c; border-radius: 3px;
    padding: 1px 6px; margin: 0 4px 4px 0;
    font-family: ui-monospace, Menlo, monospace; font-size: 8.5px;
  }
  .hit-reason { color: #7f1d1d; font-size: 10px; margin-top: 3px; }
  ul.factors, ul.ownership { padding-left: 16px; margin: 0; }
  ul.factors li, ul.ownership li { margin-bottom: 4px; page-break-inside: avoid; }
  .level {
    font-family: ui-monospace, Menlo, monospace; font-size: 8.5px;
    letter-spacing: 0.12em; text-transform: uppercase; font-weight: 700; margin-right: 6px;
  }
  .psa { color: #a3a3a3; font-size: 9.5px; }
  footer {
    margin-top: 26px; border-top: 1px solid #e5e5e5; padding-top: 8px;
    display: flex; justify-content: space-between; color: #a3a3a3;
    font-family: ui-monospace, Menlo, monospace; font-size: 8.5px;
    letter-spacing: 0.1em; text-transform: uppercase;
  }
</style>
</head>
<body>
  <header>
    <div class="kicker">Entity Risk Resolver · Compliance Risk Report</div>
    <h1>${esc(summary.entity_name)}<span class="status">${summary.found ? "identified" : "not found"}</span></h1>
    <div class="meta">
      Generated ${esc(date)}${summary.entity_id ? ` · entity …${esc(summary.entity_id.slice(-12))}` : ""}
      · Sources: Sayari, ICIJ Offshore Leaks, OpenSanctions
    </div>
    ${signals ? `<div class="signals">${signals}</div>` : ""}
  </header>

  <h2>Risk profile</h2>
  <div class="narrative">${md(summary.investigation_summary)}</div>

  ${claims ? `<h2>Findings (${summary.claims.length} sourced claims)</h2>${claims}` : ""}

  ${sanctions ? `<h2>Sanctions &amp; watchlist hits</h2>${sanctions}` : ""}

  ${ownershipRows ? `<h2>Ownership &amp; control highlights</h2><ul class="ownership">${ownershipRows}</ul>` : ""}

  ${factors ? `<h2>Sayari risk factors</h2><ul class="factors">${factors}</ul>` : ""}

  <footer>
    <span>Generated by Entity Risk Resolver</span>
    <span>${esc(date)} · Tools: ${esc(summary.tools_used.join(", "))}</span>
  </footer>
</body>
</html>`;
}

/**
 * Open the print-styled report in a new window and trigger the native print
 * dialog (where "Save as PDF" lives). Returns false when the popup was
 * blocked so the caller can tell the user.
 */
export function downloadRiskReportPdf(
  summary: RiskSummary,
  nodesById?: Map<string, GraphNode>
): boolean {
  const w = window.open("", "_blank", "width=900,height=1100");
  if (!w) return false;
  w.document.open();
  w.document.write(buildRiskReportHtml(summary, nodesById));
  w.document.close();
  // Let the new document lay out before printing; print() blocks on the dialog.
  w.focus();
  window.setTimeout(() => {
    try {
      w.print();
    } catch {
      /* window already closed */
    }
  }, 250);
  return true;
}
