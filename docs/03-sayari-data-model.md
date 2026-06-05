# Sayari Data Model

Ground-truth reference for the Sayari Graph API, built from the live API (our
`list_1` entities) plus the published ontology. This is what we design the data
layer, tools, and ICIJ overlap against.

Source of truth: live calls in `backend/sayari_explore.py`, OpenAPI in the
uploaded `search-entity` reference, and the ontology pages
(`/sayari-library/ontology/{entities,relationships,attributes}`).

---

## 1. The three calls we actually use

| Call | SDK | Returns | Use |
| --- | --- | --- | --- |
| Resolution | `client.resolution.resolution(name=, address=, country=)` | ranked candidate list (`data[]`) | turn a raw name/address into a Sayari `entity_id` |
| Entity | `client.entity.get_entity(id)` | full `EntityDetails` | the profile: risk, identifiers, attributes, relationships |
| Traversal | `client.traversal.{watchlist,ubo,ownership,shortest_path}(id)` | relationship paths | walk the ownership/network graph |

Auth, token rotation, and 429 retry-after are handled inside the SDK. We do not
hand-roll any of it.

### Resolution response shape (`data[0]`)

```
profile: "corporate"
score: 225.12917          # relevance rank, descending. NOT a 0-1 confidence
entity_id: "RZAPsBRdYXTToVqy4ZuNow"
label: "ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО \"ГАЗПРОМ\""
type: "company"
identifiers: [ {value, type, label}, ... ]   # OFAC SDN #, LEI, SEC CIK, ru_inn...
```

Key finding: `score` ranks matches but **the top score is not always the
canonical entity.** Searching "Sberbank" at the real HQ address returned the
subsidiary *Sberbank-Service* first; the parent *Sberbank Rossii* ranked third.
There is no populated `match_strength` in this SDK response. So disambiguation =
score + address + identifiers, and the agent still has to reason about whether
`data[0]` is the intended target. Same discipline as the Jeffrey-Lipman case on
the ICIJ side.

---

## 2. EntityDetails — the profile

Top-level fields (from the OpenAPI `EntityDetails` schema, confirmed live):

**Summary / flags**
- `id`, `label`, `translated_label`, `type` (entity type, §6)
- `sanctioned: bool` — true iff the entity carries the "Sanctioned" risk factor
- `pep: bool` — true iff it carries the PEP risk factor
- `psa_count: int` — number of "Possibly the Same As" entities (Sayari's own ER)
- `degree: int` — outgoing relationship count (Gazprom = 15,003)
- `closed: bool` — entity no longer exists
- `countries: [ISO trigram]` (Gazprom touches 10 countries)
- `company_type`, `latest_status {status, date}`, `registration_date`

**Hard match keys**
- `identifiers: [{value, type, label}]` — `usa_ofac_sdn_number`, `lei`,
  `usa_sec_cik_number`, `ru_inn/ru_tin/ru_ogrn`, `open_sanctions_internal_id`,
  `ukr_sanctions_nazk_*`, etc. These are the strong join keys for cross-source
  matching (much better than name).

**Counts (cheap network summary without pulling the graph)**
- `relationship_count: {rel_type: int}` — e.g. `has_shareholder: 357`,
  `subsidiary_of: 1`, `awarder_of: 3616`, `procures_from: 1243`
- `attribute_count` / `attribute_counts: {attr_type: int}`
- `trade_count: {sent, received}` — shipment volume
- `related_entities_count`, `source_count: {sourceId: {count, label}}`

**Heavy nested objects** (paginated — fetch deliberately, §3-4)
- `risk` — the headline. §3.
- `attributes` — §4.
- `relationships` — §5.
- `possibly_same_as` — Sayari's entity-resolution candidates (mirrors our
  `find_er_links` on ICIJ).
- `referenced_by`, `addresses`.

---

## 3. Risk model (the headline)

`risk` is a **map of `risk_factor_name → {value, metadata, level}`.** This is
the most valuable thing Sayari gives us and it maps one-to-one onto our existing
claim/provenance model.

```
risk: {
  "owner_of_sanctioned_eu_ec_regulation_833_2014_entity": {
    "value": 2.0,
    "level": "high",
    "metadata": { "traversal_path": ["GAZPROM|has_subsidiary|X|owner_of|Y"] }
  },
  "state_owned": { "value": true, "level": "high", "metadata": {...} },
  ...
}
```

**`level`** (severity, enum): `critical` > `high` > `elevated` > `relevant`.

**`value`** is `string | number | bool`:
- `true` for direct/categorical factors (`sanctioned`, `state_owned`, `usa_bis`).
- a **number that equals the hops in the ownership chain** for derived factors:
  `1.0` = direct owner/owned, `2.0`/`3.0` = 2-3 relationships away. (Confirmed:
  the numeric value matches the length of `traversal_path`.)
- a score for index-style factors (`cpi_score: 22.0`, `basel_aml: 5.35`).

**`metadata.traversal_path`** = list of `srcId|rel|tgtId|rel|tgtId` strings. This
is the *why*: the exact ownership/control chain that triggered the risk. It
draws directly onto our React Flow graph and becomes a claim with Sayari as the
source.

### Risk-factor taxonomy (naming is systematic)

Sberbank-Service carried **44** risk factors; Gazprom carried **95**. They follow
a predictable grammar:

| Pattern | Meaning | Example |
| --- | --- | --- |
| `sanctioned`, `sanctioned_<authority>` | directly sanctioned, per list | `sanctioned_usa_ofac_sdn`, `sanctioned_can_gac`, `sanctioned_ukr_nsdc` |
| `owned_by_sanctioned_<auth>_entity` | a sanctioned entity owns it (up the chain) | `owned_by_sanctioned_usa_ofac_sdn_entity` |
| `owner_of_sanctioned_<auth>_entity` | it owns a sanctioned entity (down the chain) | `owner_of_sanctioned_gbr_fcdo_entity` |
| `controlled_by_<juris>_sanctioned` | control via a sanctioned director/officer | `controlled_by_ofac_sdn` |
| `psa_owned_by_*` / `psa_owner_of_*` | same as above but via a *Possibly Same As* link (weaker, ER-derived) | `psa_owned_by_soe` |
| `state_owned`, `owned_by_soe`, `owner_of_soe`, `soe_adjacent` | state ownership | `state_owned` |
| export/regulatory | `export_controls`, `usa_bis`, `meu_list_contractors`, `regulatory_action`, `law_enforcement_action` | |
| contextual scores | `pep_adjacent`, `eu_high_risk_third`, `cpi_score`, `basel_aml`, `reputational_risk_financial_crime` | |

Design consequence: **never dump the full `risk` map to the LLM.** A Gazprom-size
record (95 factors, each with a path) would blow the context window and trigger
the 429s we already fought. We slim to: counts by level, the direct
`sanctioned*`/`state_owned`/`export_controls` factors verbatim, and the top N
ownership-derived factors *with* their `traversal_path`. The `psa_*` factors get
flagged as lower-confidence (ER-derived) rather than treated as hard hits — that
is exactly the Jeffrey-Lipman / "match on strong keys" discipline.

---

## 4. Attributes model

`attributes` is keyed by attribute type, each value paginated and
**provenance-bearing**:

```
attributes.address: {
  limit: 100,
  size: { count: 8, qualifier: "eq" },
  data: [ { record: ["<sourceId>/<docId>/<ts>"], record_count: 445,
            properties: { city, country, road, house_number, ... } } ]
}
```

- Each attribute datum carries `record` ids → traceable back to a source
  document. That is the provenance chain our source-chips already render.
- Attribute types we see live: `additional_information`, `address`,
  `business_purpose`, `company_type`, `country`, `financials`, `identifier`,
  `name`, `risk_intelligence`, `shares`, `status`.
- `address` is parsed to libpostal fields (road, house_number, city, state,
  postcode, x/y coords) plus `translated`/`transliterated` — strong for address
  matching against ICIJ addresses.
- `financials` is deep (revenue, assets, employees, paid_up_capital, ~40 fields).
- `business_purpose` carries NAICS/NACE/ISIC codes.

---

## 5. Relationships model

```
relationships: {
  data: [ { target: { ...EntityDetails-lite: id,label,type,sanctioned,pep,... },
            <relationship attrs> } ],
  limit, next, size
}
```

- Each related `target` is itself a mini entity summary carrying its own
  `sanctioned`/`pep` flags and identifiers — so one hop already tells us if a
  neighbor is risky.
- Paginated via `next`. We pull on demand (graph expansion), not all at once.

---

## 6. Ontology (condensed)

**Entity types:** `company`, `person`, `government_organization`, `contract`,
`shipment`, `vessel`, `aircraft`, `property`, `intellectual_property`,
`security`, `account`, `transaction`, `legal_matter`, plus `generic`/`unknown`.

**Ownership / control relationships** (the ones that matter for risk traversal):
`shareholder_of` / `has_shareholder`, `beneficial_owner_of` /
`has_beneficial_owner`, `subsidiary_of` / `has_subsidiary`, `owner_of` /
`has_owner`, `director_of`, `officer_of`, `manager_of`, `member_of_the_board_of`,
`partner_of`, `legal_successor_of`, `branch_of`, `family_of`, `linked_to`.

**Trade / contracts:** `ships_to` / `receives_from`, `shipper_of`, `carrier_of`,
`receiver_of`, `awarder_of` / `recipient_of`, `procures_from`.

Each relationship has a reverse name and `former: bool` (relationship no longer
exists) plus `from_date`/`to_date`.

---

## 7. ICIJ ⇄ Sayari overlap (why keep both)

| Dimension | ICIJ (Neo4j) | Sayari |
| --- | --- | --- |
| What it is | leaked offshore incorporation records (Panama/Paradise/Pandora Papers) | aggregated global registries + sanctions + trade + watchlists |
| Nodes | `Entity, Officer, Intermediary, Address, Other` | `company, person, government_organization, contract, shipment, ...` |
| Ownership edges | `officer_of`, `intermediary_of`, `registered_address`, `underlying` | `shareholder_of`, `beneficial_owner_of`, `subsidiary_of`, `owner_of`, ... |
| Entity resolution | `same_id_as`, `same_as`, `same_company_as`, `same_name_as` | `possibly_same_as` / `psa_count` |
| Sanctions / risk | none (we bolt on OpenSanctions) | native, scored, with traversal paths |
| Identifiers | sparse | rich (OFAC SDN, LEI, SEC CIK, national reg numbers) |
| Coverage | leak-limited | broad, current |
| Unique value | **leak provenance** — "appears in the Pandora Papers" is a story Sayari does not tell | breadth, risk scoring, trade, freshness |

The overlap is the **graph shape** (both model directors/owners/addresses). The
differentiators are complementary: Sayari supplies authoritative risk +
identifiers + breadth; ICIJ supplies the leak context. Keeping both lets the
agent corroborate across independent sources (the multi-source story) and surface
"this Sayari-resolved entity *also* appears in the Panama Papers," which is
exactly the kind of finding the demo should produce.

**Join strategy:** resolve on Sayari first (best identifiers), then match into
ICIJ on strong keys — resolved canonical name + country, or a shared registered
address — and let the agent judge whether the ICIJ hit is the same entity. Do
not auto-merge on name alone.

---

## 8. Design implications (for the build)

1. **Data layer** — `app/sayari.py` wrapping `resolution`, `get_entity`,
   `traversal.*`, structured like `neo4j_client.py` / `sanctions.py`.
2. **Slim aggressively before the LLM** — risk: counts-by-level + direct factors
   + top-N derived factors with paths. Never the raw 95-factor map.
3. **Resolution returns candidates, not an answer** — surface top matches; agent
   picks using score + address + identifiers.
4. **Map risk → claims** — each surfaced risk factor becomes a claim; its
   `traversal_path` is the provenance and the graph overlay; `psa_*` factors are
   tagged lower-confidence.
5. **Cross-source corroboration** — after resolving on Sayari, join into ICIJ +
   OpenSanctions on strong keys for the "multiple independent sources" payoff.
