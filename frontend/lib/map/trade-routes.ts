/**
 * Trade-route extraction for the map view. Self-contained on purpose: the
 * lmcanvas reskin can restyle TradeRoutesMap without touching the store.
 *
 * Primary source: each `sayari_trade` tool result carries a compact
 * `metadata.routes` array (built backend-side by sayari.shipments_to_routes),
 * which rides the existing tool_call_result SSE event into each turn's
 * `toolCalls[].resultMeta`. We take the LATEST result per (entity_id, role)
 * query so re-running the same trade question never double-counts, then merge
 * across distinct queries by country pair.
 *
 * Fallback (page reload): hydration restores the graph but not toolCalls, so
 * when no metadata routes exist we derive coarse routes from the persisted
 * `ships_to` edges, using each party node's own country. Less precise than
 * shipment departure/arrival, but keeps the map alive after a refresh.
 */

import type { Turn } from "@/lib/conversation-store";
import type { GraphEdge, GraphNode } from "@/lib/types";

export interface TradeRoute {
  departure_country: string; // ISO-3
  arrival_country: string; // ISO-3
  shipment_count: number;
  total_value: number | null;
  dual_use: boolean;
  sanctioned_party: boolean;
  hs_codes: string[];
}

function isTradeRoute(r: unknown): r is TradeRoute {
  if (!r || typeof r !== "object") return false;
  const o = r as Record<string, unknown>;
  return (
    typeof o.departure_country === "string" && typeof o.arrival_country === "string"
  );
}

function mergeInto(acc: Map<string, TradeRoute>, r: TradeRoute) {
  const key = `${r.departure_country}->${r.arrival_country}`;
  const prev = acc.get(key);
  if (!prev) {
    acc.set(key, { ...r, hs_codes: [...(r.hs_codes ?? [])] });
    return;
  }
  prev.shipment_count += r.shipment_count;
  if (r.total_value != null) prev.total_value = (prev.total_value ?? 0) + r.total_value;
  prev.dual_use = prev.dual_use || r.dual_use;
  prev.sanctioned_party = prev.sanctioned_party || r.sanctioned_party;
  for (const c of r.hs_codes ?? []) {
    if (!prev.hs_codes.includes(c) && prev.hs_codes.length < 5) prev.hs_codes.push(c);
  }
}

/** Routes from sayari_trade tool-result metadata across all turns. */
function routesFromToolCalls(turns: Turn[]): TradeRoute[] {
  // Latest result wins per query key, so a re-run replaces instead of stacking.
  const byQuery = new Map<string, TradeRoute[]>();
  for (const turn of turns) {
    for (const call of turn.toolCalls) {
      if (call.tool !== "sayari_trade" || !call.hasResult) continue;
      const raw = (call.resultMeta as Record<string, unknown> | undefined)?.routes;
      if (!Array.isArray(raw)) continue;
      const routes = raw.filter(isTradeRoute).map((r) => ({
        departure_country: String(r.departure_country).toUpperCase(),
        arrival_country: String(r.arrival_country).toUpperCase(),
        shipment_count: Number(r.shipment_count) || 1,
        total_value: r.total_value == null ? null : Number(r.total_value),
        dual_use: r.dual_use === true,
        sanctioned_party: r.sanctioned_party === true,
        hs_codes: Array.isArray(r.hs_codes) ? r.hs_codes.map(String) : [],
      }));
      const qkey = `${call.args?.entity_id ?? ""}|${call.args?.role ?? "supplier"}`;
      byQuery.set(qkey, routes);
    }
  }
  const acc = new Map<string, TradeRoute>();
  for (const routes of byQuery.values()) for (const r of routes) mergeInto(acc, r);
  return Array.from(acc.values());
}

/** Coarse fallback: party-country pairs off persisted ships_to graph edges. */
function routesFromGraph(
  nodes: Map<string, GraphNode>,
  edges: Map<string, GraphEdge>
): TradeRoute[] {
  const countryOf = (id: string): string | null => {
    const props = (nodes.get(id)?.properties ?? {}) as Record<string, unknown>;
    const cs = props.countries;
    return Array.isArray(cs) && cs.length ? String(cs[0]).toUpperCase() : null;
  };
  const acc = new Map<string, TradeRoute>();
  for (const e of edges.values()) {
    if (e.type !== "ships_to") continue;
    const dep = countryOf(e.source);
    const arr = countryOf(e.target);
    if (!dep || !arr) continue;
    const p = (e.properties ?? {}) as Record<string, unknown>;
    const srcProps = (nodes.get(e.source)?.properties ?? {}) as Record<string, unknown>;
    const tgtProps = (nodes.get(e.target)?.properties ?? {}) as Record<string, unknown>;
    mergeInto(acc, {
      departure_country: dep,
      arrival_country: arr,
      shipment_count: Number(p.shipment_count) || 1,
      total_value: p.value == null ? null : Number(p.value),
      dual_use: p.dual_use === true,
      sanctioned_party: srcProps.sanctioned === true || tgtProps.sanctioned === true,
      hs_codes: Array.isArray(p.hs_codes) ? p.hs_codes.map(String).slice(0, 5) : [],
    });
  }
  return Array.from(acc.values());
}

/**
 * All trade routes known to the current conversation, most valuable first.
 * Metadata routes (precise shipment geography) win; the graph-edge fallback
 * only kicks in when no metadata survived (e.g. after a page reload).
 */
export function collectTradeRoutes(
  turns: Turn[],
  nodes: Map<string, GraphNode>,
  edges: Map<string, GraphEdge>
): TradeRoute[] {
  const primary = routesFromToolCalls(turns);
  const routes = primary.length ? primary : routesFromGraph(nodes, edges);
  return routes.sort(
    (a, b) => (b.total_value ?? 0) - (a.total_value ?? 0) || b.shipment_count - a.shipment_count
  );
}
