/**
 * Known-entity name matching shared by the markdown strong-renderer (clickable
 * entity names) and the composer autocomplete. An entity is "known" when its
 * name matches a graph node or a state_doc registry entry after normalization.
 *
 * Matching is robust to three things that otherwise sink the Gazprom-style runs
 * (where prose, the Sayari registry, and OpenSanctions all spell the same
 * entity differently):
 *   1. Cyrillic vs Latin: names are transliterated to a Latin baseline, so
 *      "ГАЗПРОМ", "Gazprom", and the OpenSanctions "GAZPROM" all converge.
 *   2. Corporate-form noise: leading/trailing legal forms (ПАО/PAO,
 *      ОАО/OAO, ООО/OOO, "publichnoe aktsionernoe obshchestvo", LLC, Ltd, …)
 *      are stripped to an alias so prose "Gazprom" matches the registry's
 *      "ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО ГАЗПРОМ".
 *   3. Casing / punctuation: guillemets, quotes, and case are flattened.
 *
 * False positives are guarded by (a) a minimum normalized length, (b) a
 * stopword list so a bare legal/generic word never becomes a match, and (c)
 * aliases that collide across two distinct entities being dropped entirely.
 */

import type { GraphNode, RegistryEntity } from "./types";

export interface EntityMatch {
  /** Display name (graph node name, falling back to the registry label). */
  name: string;
  /** Evidence-graph node id, when the entity is on the graph. */
  nodeId?: string;
  /** Registry id (Sayari / sanctions id), when the entity is in the registry. */
  registryId?: string;
}

/**
 * Cyrillic -> Latin (BGN/PCGN-ish, lowercase). Deliberately matches the
 * romanization OpenSanctions/Sayari emit (e.g. "ШЕЛЬФПРОЕКТ" -> "shelfproekt",
 * "ГАЗПРОМ НЕФТЬ" -> "gazprom neft") so a Cyrillic registry label and a Latin
 * sanctions label for the same entity normalize to the same key.
 */
const CYRILLIC_TO_LATIN: Record<string, string> = {
  а: "a", б: "b", в: "v", г: "g", д: "d", е: "e", ё: "e", ж: "zh", з: "z",
  и: "i", й: "y", к: "k", л: "l", м: "m", н: "n", о: "o", п: "p", р: "r",
  с: "s", т: "t", у: "u", ф: "f", х: "kh", ц: "ts", ч: "ch", ш: "sh",
  щ: "shch", ъ: "", ы: "y", ь: "", э: "e", ю: "yu", я: "ya",
  // Ukrainian / Belarusian extras seen in the demo data.
  і: "i", ї: "yi", є: "ye", ґ: "g", ў: "u",
};

function transliterate(s: string): string {
  let out = "";
  for (const ch of s) {
    const mapped = CYRILLIC_TO_LATIN[ch];
    out += mapped !== undefined ? mapped : ch;
  }
  return out;
}

/**
 * Case-insensitive normalized form of an entity name: lowercased, Cyrillic
 * transliterated to Latin, diacritics stripped, punctuation collapsed to single
 * spaces. "Rosneft Trading S.A." and "rosneft trading sa" normalize to the same
 * key; "ГАЗПРОМ" and "Gazprom" both become "gazprom".
 */
export function normalizeEntityName(s: string): string {
  return transliterate(s.toLowerCase())
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

/**
 * Leading corporate/legal forms, normalized (transliterated + lowercased).
 * Longest first so "publichnoe aktsionernoe obshchestvo" wins over "obshchestvo".
 */
const PREFIX_FORMS = [
  "obshchestvo s ogranichennoy otvetstvennostyu",
  "publichnoe aktsionernoe obshchestvo",
  "otkrytoe aktsionernoe obshchestvo",
  "zakrytoe aktsionernoe obshchestvo",
  "aktsionernoe obshchestvo",
  "pao", "oao", "ooo", "zao", "ao",
  "pjsc", "ojsc", "cjsc", "jsc", "ojs",
  "llc", "ltd", "plc", "inc", "gmbh", "spa", "sarl", "sas",
  "ag", "bv", "nv", "oy", "as",
];

/** Trailing corporate/legal forms, normalized. */
const SUFFIX_FORMS = [
  "llc", "ltd", "plc", "inc", "gmbh", "spa", "sarl", "sas", "ag", "bv", "nv",
  "co", "corp", "limited", "company", "holding", "holdings",
];

/**
 * Generic tokens that must never stand alone as a match (they'd light up on
 * common prose). A single-token key in this set is rejected as both an alias
 * and a query.
 */
const STOPWORDS = new Set([
  "the", "and", "company", "holding", "holdings", "group", "entity", "officer",
  "director", "shareholder", "owner", "subsidiary", "parent", "sanctions",
  "sanctioned", "limited", "corporation", "trust", "fund", "bank", "trading",
]);

function tokens(key: string): string[] {
  return key ? key.split(" ").filter(Boolean) : [];
}

/** A key is usable for matching only if it's long enough and not a lone stopword. */
function isUsableKey(key: string): boolean {
  if (key.length < 3) return false;
  const toks = tokens(key);
  if (toks.length === 1 && STOPWORDS.has(toks[0])) return false;
  return true;
}

/**
 * Alias keys for a normalized name: the corporate-form-stripped variants. Used
 * on BOTH sides: aliases are indexed at build time, and a query is also tried
 * against its own aliases, so "PAO Gazprom", "Gazprom", and "ПАО «Газпром»"
 * all reach "gazprom". Excludes the full key itself; callers add that.
 */
export function aliasKeys(fullKey: string): string[] {
  const out = new Set<string>();
  const consider = (k: string) => {
    const t = k.trim();
    if (t && t !== fullKey && isUsableKey(t)) out.add(t);
  };

  // Strip a leading legal form.
  let lead = fullKey;
  for (const form of PREFIX_FORMS) {
    if (lead === form) break;
    if (lead.startsWith(form + " ")) {
      lead = lead.slice(form.length + 1).trim();
      consider(lead);
      break;
    }
  }
  // Strip a trailing legal form (from the full and the lead-stripped form).
  for (const base of [fullKey, lead]) {
    const toks = tokens(base);
    if (toks.length >= 2 && SUFFIX_FORMS.includes(toks[toks.length - 1])) {
      consider(toks.slice(0, -1).join(" "));
    }
  }
  return Array.from(out);
}

/**
 * Build the normalized-name -> match lookup from the two client-side sources.
 * Full names are authoritative; corporate-form aliases are added only when they
 * unambiguously point at a single entity (collisions are dropped so a click
 * never opens the wrong entity).
 */
export function buildEntityLookup(
  nodes: Map<string, GraphNode>,
  registry: Map<string, RegistryEntity>
): Map<string, EntityMatch> {
  const lookup = new Map<string, EntityMatch>();

  const upsertFull = (
    key: string,
    patch: { name: string; nodeId?: string; registryId?: string }
  ) => {
    const existing = lookup.get(key);
    if (existing) {
      if (patch.nodeId) {
        existing.nodeId = patch.nodeId;
        existing.name = patch.name; // prefer the on-graph spelling
      }
      if (patch.registryId && !existing.registryId) existing.registryId = patch.registryId;
    } else {
      lookup.set(key, { ...patch });
    }
  };

  // Registry first (so a graph node sharing the key can override the spelling).
  for (const [id, rec] of registry) {
    const label = rec.label?.trim();
    if (!label) continue;
    const key = normalizeEntityName(label);
    if (!isUsableKey(key)) continue;
    upsertFull(key, { name: label, registryId: id });
  }
  for (const node of nodes.values()) {
    const key = normalizeEntityName(node.name);
    if (!isUsableKey(key)) continue;
    upsertFull(key, { name: node.name, nodeId: node.id });
  }

  // Pass 2: corporate-form aliases. Record every alias -> owning full key, then
  // promote only the unambiguous ones (one distinct owner, not already a full key).
  const aliasOwners = new Map<string, Set<string>>();
  const aliasMatch = new Map<string, EntityMatch>();
  for (const [fullKey, match] of lookup) {
    for (const alias of aliasKeys(fullKey)) {
      if (lookup.has(alias)) continue; // a real full name always wins
      const owners = aliasOwners.get(alias) ?? new Set<string>();
      owners.add(fullKey);
      aliasOwners.set(alias, owners);
      aliasMatch.set(alias, match);
    }
  }
  for (const [alias, owners] of aliasOwners) {
    if (owners.size === 1 && !lookup.has(alias)) {
      lookup.set(alias, aliasMatch.get(alias)!);
    }
  }

  return lookup;
}

/**
 * Resolve a (possibly messy) display name to a known entity. Tries the exact
 * normalized key first, then the name's corporate-form aliases, so prose that
 * adds or drops a legal form still resolves. Returns null when nothing matches
 * or the name normalizes to an unusable (too-short / stopword) key.
 */
export function matchEntity(
  lookup: Map<string, EntityMatch>,
  name: string
): EntityMatch | null {
  const key = normalizeEntityName(name);
  if (!isUsableKey(key)) return null;
  const direct = lookup.get(key);
  if (direct) return direct;
  for (const alias of aliasKeys(key)) {
    const hit = lookup.get(alias);
    if (hit) return hit;
  }
  return null;
}
