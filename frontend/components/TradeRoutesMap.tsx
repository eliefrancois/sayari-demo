"use client";

import { useMemo, useState } from "react";
import {
  ComposableMap,
  Geographies,
  Geography,
  Line,
  Marker,
  ZoomableGroup,
} from "react-simple-maps";

import centroidsJson from "@/lib/map/country-centroids.json";
import type { TradeRoute, TradeSubject } from "@/lib/map/trade-routes";

/**
 * Risky-routes world map (Tier 2). Country-level arcs departure -> arrival
 * between bundled ISO3 centroids, react-simple-maps "basic markers" style.
 * Arc color encodes risk: sanctioned party red, dual-use amber, clean neutral.
 * Stroke width scales with shipment count, opacity with lane value.
 *
 * Self-contained by design (own component + lib/map assets) so the upcoming
 * lmcanvas reskin can restyle it without surgery. Styling is functional, not
 * polished, for the same reason.
 */

const GEO_URL = "/maps/countries-110m.json";
// JSON imports type as number[]; the asset is generated as [lng, lat] pairs.
const CENTROIDS = centroidsJson as unknown as Record<string, [number, number]>;

const COLOR_SANCTIONED = "rgb(248 113 113)"; // red — a directly sanctioned party on the lane
const COLOR_DUAL_USE = "rgb(251 191 36)"; // amber — HS screen or native BIS tag fired
const COLOR_CLEAN = "rgb(113 113 122)"; // zinc — no risk signal
const COLOR_MARKER = "rgb(45 212 191)"; // teal — Sayari accent, matches the graph legend

function routeColor(r: TradeRoute): string {
  if (r.sanctioned_party) return COLOR_SANCTIONED;
  if (r.dual_use) return COLOR_DUAL_USE;
  return COLOR_CLEAN;
}

function strokeWidth(count: number): number {
  return Math.min(0.75 + Math.log2(1 + count) * 0.6, 4);
}

function valueOpacity(value: number | null, max: number): number {
  if (!value || max <= 0) return 0.55;
  return 0.45 + 0.45 * Math.min(value / max, 1);
}

function fmtValue(v: number | null): string {
  if (v == null) return "value n/a";
  if (v >= 1e9) return `$${(v / 1e9).toFixed(1)}B`;
  if (v >= 1e6) return `$${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `$${(v / 1e3).toFixed(0)}K`;
  return `$${v.toFixed(0)}`;
}

type HoverState = { route: TradeRoute; x: number; y: number } | null;

function subjectLine(s: TradeSubject): string {
  return s.role === "buyer" ? `${s.name} · imports (buyer)` : `${s.name} · exports (supplier)`;
}

/** "Subject → Counterparty" tooltip lead, direction-aware. With multiple
 * subjects the merged lanes can't be attributed to one query, so we drop the
 * subject and show counterparties alone. */
function partiesLine(
  route: TradeRoute,
  subjects: TradeSubject[]
): string | null {
  if (route.top_parties.length === 0) return null;
  const parties = route.top_parties.join(", ");
  if (subjects.length !== 1) return `Counterparties: ${parties}`;
  const s = subjects[0];
  return s.role === "buyer" ? `${parties} → ${s.name}` : `${s.name} → ${parties}`;
}

export function TradeRoutesMap({
  routes,
  subjects = [],
}: {
  routes: TradeRoute[];
  subjects?: TradeSubject[];
}) {
  const [hover, setHover] = useState<HoverState>(null);

  // Arcs need both endpoints in the centroid table; domestic (dep === arr)
  // lanes have no arc to draw and surface as a marker only.
  const { arcs, markers, skipped } = useMemo(() => {
    const arcs: TradeRoute[] = [];
    const countries = new Set<string>();
    let skipped = 0;
    for (const r of routes) {
      const from = CENTROIDS[r.departure_country];
      const to = CENTROIDS[r.arrival_country];
      if (!from || !to) {
        skipped++;
        continue;
      }
      countries.add(r.departure_country);
      countries.add(r.arrival_country);
      if (r.departure_country !== r.arrival_country) arcs.push(r);
    }
    const markers = Array.from(countries).map((iso3) => ({
      iso3,
      coordinates: CENTROIDS[iso3],
    }));
    return { arcs, markers, skipped };
  }, [routes]);

  const maxValue = useMemo(
    () => Math.max(0, ...arcs.map((r) => r.total_value ?? 0)),
    [arcs]
  );

  const hasRisk = arcs.some((r) => r.dual_use || r.sanctioned_party);

  if (routes.length === 0) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-zinc-950 text-center">
        <div>
          <div className="text-sm text-zinc-600">No trade routes yet</div>
          <div className="mt-1 text-xs text-zinc-700">
            Ask about an entity&apos;s shipments (exports/imports) and the lanes
            will appear here.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div
      className="relative h-full w-full overflow-hidden bg-zinc-950"
      onMouseLeave={() => setHover(null)}
    >
      <ComposableMap
        projection="geoEqualEarth"
        projectionConfig={{ scale: 160 }}
        style={{ width: "100%", height: "100%" }}
      >
        <ZoomableGroup center={[10, 10]} zoom={1} minZoom={0.8} maxZoom={6}>
          <Geographies geography={GEO_URL}>
            {({ geographies }) =>
              geographies.map((geo) => (
                <Geography
                  key={geo.rsmKey}
                  geography={geo}
                  fill="rgb(39 39 42)"
                  stroke="rgb(63 63 70)"
                  strokeWidth={0.4}
                  style={{
                    default: { outline: "none" },
                    hover: { outline: "none", fill: "rgb(52 52 56)" },
                    pressed: { outline: "none" },
                  }}
                />
              ))
            }
          </Geographies>

          {arcs.map((r) => (
            <Line
              key={`${r.departure_country}-${r.arrival_country}`}
              from={CENTROIDS[r.departure_country]}
              to={CENTROIDS[r.arrival_country]}
              stroke={routeColor(r)}
              strokeWidth={strokeWidth(r.shipment_count)}
              strokeLinecap="round"
              style={{
                opacity: valueOpacity(r.total_value, maxValue),
                cursor: "pointer",
              }}
              onMouseMove={(evt: React.MouseEvent<SVGPathElement>) => {
                const rect = (
                  evt.currentTarget.ownerSVGElement?.parentElement ?? evt.currentTarget
                ).getBoundingClientRect();
                setHover({
                  route: r,
                  x: evt.clientX - rect.left,
                  y: evt.clientY - rect.top,
                });
              }}
              onMouseLeave={() => setHover(null)}
            />
          ))}

          {markers.map((m) => (
            <Marker key={m.iso3} coordinates={m.coordinates}>
              <circle r={2.4} fill={COLOR_MARKER} stroke="rgb(9 9 11)" strokeWidth={0.8} />
              <text
                textAnchor="middle"
                y={-5}
                style={{
                  fontFamily: "ui-sans-serif, system-ui",
                  fontSize: 6,
                  fill: "rgb(212 212 216)",
                  pointerEvents: "none",
                }}
              >
                {m.iso3}
              </text>
            </Marker>
          ))}
        </ZoomableGroup>
      </ComposableMap>

      {/* Subject header: whose trade these lanes belong to, and which direction. */}
      {subjects.length > 0 && (
        <div className="pointer-events-none absolute left-3 top-3 z-10 max-w-sm rounded-md border border-zinc-800 bg-zinc-900/85 px-2.5 py-1.5 text-[11px] shadow-lg backdrop-blur">
          <div className="font-medium uppercase tracking-wider text-zinc-500">
            Showing trade for
          </div>
          <div className="mt-0.5 flex flex-col gap-0.5 text-zinc-200">
            {subjects.map((s) => (
              <span key={`${s.entity_id}|${s.role}`}>{subjectLine(s)}</span>
            ))}
          </div>
        </div>
      )}

      {/* Legend (matches the graph panel's bottom-left convention). */}
      <div className="pointer-events-none absolute bottom-3 left-3 z-10 rounded-md border border-zinc-800 bg-zinc-900/85 px-2.5 py-1.5 text-[10px] shadow-lg backdrop-blur">
        <div className="mb-1 font-medium uppercase tracking-wider text-zinc-500">
          Trade routes
        </div>
        <div className="flex flex-col gap-1 text-zinc-300">
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-0 w-3 border-t-2" style={{ borderColor: COLOR_SANCTIONED }} />
            Sanctioned party on lane
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-0 w-3 border-t-2" style={{ borderColor: COLOR_DUAL_USE }} />
            Dual-use flagged
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-0 w-3 border-t-2" style={{ borderColor: COLOR_CLEAN }} />
            No risk signal
          </span>
          <span className="text-zinc-500">width = shipments · opacity = value</span>
        </div>
      </div>

      {/* Route count / skipped badge. */}
      <div className="pointer-events-none absolute right-3 top-3 z-10 rounded-md border border-zinc-800 bg-zinc-900/85 px-2.5 py-1 text-[11px] text-zinc-400 shadow-lg backdrop-blur">
        {arcs.length} route{arcs.length === 1 ? "" : "s"}
        {hasRisk && <span className="ml-1.5 text-amber-300">· risk flagged</span>}
        {skipped > 0 && (
          <span className="ml-1.5 text-zinc-600">· {skipped} unmappable</span>
        )}
      </div>

      {hover && (
        <div
          className="pointer-events-none absolute z-20 max-w-xs rounded-md border border-zinc-700 bg-zinc-900/95 px-3 py-2 text-xs shadow-xl backdrop-blur"
          style={{ left: hover.x + 12, top: hover.y + 12 }}
        >
          <div className="font-medium text-zinc-100">
            {hover.route.departure_country} → {hover.route.arrival_country}
          </div>
          {partiesLine(hover.route, subjects) && (
            <div className="mt-0.5 text-zinc-200">
              {partiesLine(hover.route, subjects)}
            </div>
          )}
          <div className="mt-1 text-zinc-300">
            {hover.route.shipment_count} shipment
            {hover.route.shipment_count === 1 ? "" : "s"} · {fmtValue(hover.route.total_value)}
          </div>
          {hover.route.hs_codes.length > 0 && (
            <div className="mt-0.5 text-[11px] text-zinc-400">
              HS: {hover.route.hs_codes.join(", ")}
            </div>
          )}
          {(hover.route.dual_use || hover.route.sanctioned_party) && (
            <div className="mt-1 flex gap-2 text-[11px]">
              {hover.route.sanctioned_party && (
                <span className="text-red-300">⛔ sanctioned party</span>
              )}
              {hover.route.dual_use && <span className="text-amber-300">⚠ dual-use</span>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
