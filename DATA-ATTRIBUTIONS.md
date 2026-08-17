# Data Attributions & Licenses — Travel Guild

**Travel Guild** (internal code codename: `society`) derives its catalog, geography, and
reference layers from open data sources. We comply with each source's license and credit
them here; honest provenance is a core project principle.

> **Scope note (honesty) — read this before the source table below.** This document
> describes the data sources used by the **full production system**, kept here in full
> for attribution completeness and transparency about how the real product is built.
> **This specific public showcase repo does NOT ship any of that derived data.** The
> curated OSM/SimpleMaps/OpenFlights/etc.-derived catalog is withheld (competitive moat +
> the OSM/ODbL layers would drag in redistribution obligations for data this repo doesn't
> actually contain) and replaced with a small, **hand-authored, honestly-labeled sample
> dataset** (see `README.md`'s "What's not here" section) so the code still boots and
> books a real trip end-to-end. None of the sample rows' displayed titles, prices, or
> availability in this repo are derived from any source below — every title is
> explicitly labeled `(demo data)`. A handful of internal (never-displayed) hotel IDs
> echo real hotel names for continuity with this repo's own shipped Go test suite (see
> `ARCHITECTURE.md`'s "Data honesty" section) — those IDs are not rendered to a user and
> carry entirely fictional prices/availability. `itinerario.io` is a *separate*
> consumer/production site and is **not** part of this submission.

---

## 1. Sources actually used (verified in code)

| # | Source | Used for | License | Attribution / link |
|---|--------|----------|---------|--------------------|
| 1 | **SimpleMaps World Cities** | City coordinates, population, and admin/state names; long-tail "beyond-catalog" city resolution (`worldcities_tier3/4.json`, `city_coords_worldcities.json`, `city_state.json`) | **CC BY 4.0 — attribution required** | https://simplemaps.com/data/world-cities |
| 2 | **OpenStreetMap** contributors (Geofabrik extracts) | Lodging, attractions, restaurants, transit/road feasibility — the POI catalog (`poi_catalog.json`, `poi_supplement.json`) | **ODbL 1.0** | https://www.openstreetmap.org/copyright • https://www.geofabrik.de/ |
| 3 | **OpenFlights** (`airports.dat`, `routes.dat`, `airlines.dat`) | Air-network route *existence* + airport coordinates (`air_network.json`); ~2014 vintage, used for feasibility not live schedules | Open data (route data **ODbL**; database terms per OpenFlights) | https://openflights.org/data.html |
| 4 | **Wikidata** (+ Wikipedia) | POI notability ranking and constructed reference/booking links (Q-ids, `wikidata`/`wikipedia` fields) | **CC0** (Wikidata); Wikipedia CC BY-SA for article text/links | https://www.wikidata.org/ |
| 5 | **Natural Earth** 1:50m land (via `nvkelso/natural-earth-vector`) | Land/sea geometry — the transport land-vs-sea gate (`land_polygons.json`) so we never route rail over water | **Public Domain** | https://www.naturalearthdata.com/ |
| 6 | **GDACS** — Global Disaster Alert & Coordination System | **Live** active-hazard overlay (do-not-travel on Orange/Red), free, no API key (`society/utils/emergency_feed.py`) | Free use / GDACS terms | https://www.gdacs.org/ |
| 7 | **Copernicus** Emergency Management Service — **EFFIS / GWIS / GDO** (+ GloFAS/EWDS, FWI, SPEI references) | Methodology + calibration for the **seeded** climate-hazard seasons (wildfire / drought / flood). NB: the demo uses *seeded* season tables calibrated to these products; a *live* Copernicus fetch is a documented Tier-2 swap, not running in the demo | Copernicus / EU open data terms (credit "Copernicus Emergency Management Service") | https://emergency.copernicus.eu/ |
| 8 | **NOAA** — CPC ENSO advisory + NHC (Atlantic), plus JTWC / BoM / FMS basin conventions | Seeded ENSO current-phase constant + cyclone-basin naming/severity. A live NOAA-ONI / IRI-plume fetch is a documented Tier-2 swap | US Gov **Public Domain** | https://www.cpc.ncep.noaa.gov/ • https://www.nhc.noaa.gov/ |
| 9 | **CDC** Travelers' Health (`wwwnc.cdc.gov/travel`) | Per-destination vaccination / prophylaxis slates (mandatory-cert gate vs recommended set); seeded per-country with `source_url` provenance | US Gov **Public Domain** | https://wwwnc.cdc.gov/travel |
| 10 | **Henley & Partners Passport Index** | Visa/passport-free reference baseline (validation harness + multi-passport optimization) | Reference use | https://www.henleyglobal.com/passport-index |
| 11 | **Official government immigration portals** (e.g. `travel.state.gov`, `gov.uk`, `immi.homeaffairs.gov.au`, `mofa.go.jp`, `ica.gov.sg`, EU Home Affairs, and per-country e-visa sites) | Authoritative visa-rule `source_url`s behind each seeded compliance entry | Government public information | per-entry `source_url` in `society/agents/compliance_agent.py` |

**AI-grounded enrichment.** Additional *real, currently-operating* lodging / attraction /
restaurant names are discovered via **Vertex AI Gemini** (grounded in Google Search) and
merged into the SEEDED demo catalog. Every such row is `provenance`-tagged
(`gemini-2.5-flash-lite-grounded`) and marked `simulated:true`; prices are demo estimates,
never live quotes.

**Web-search-grounded enrichment (one-off, non-pipeline).** In the full private engine, a
small number of rows (task #86: Thimphu/Bhutan, Valletta/Malta, Victoria-Mahé/Seychelles —
22 lodging rows normally living in `ucp-merchant/catalog_supplement.json`) were sourced
the same way in spirit — real, currently-operating hotel/guesthouse names cross-checked
against multiple independent listings (official hotel sites, Tripadvisor, Booking.com,
Hotels.com) via live web search — but *not* through the `seed_lodgings_vertex.py` /
Vertex-Gemini pipeline. They are `provenance`-tagged `claude-websearch-grounded` (a
distinct tag, so the sourcing mechanism is never misattributed to the Gemini pipeline)
and marked `simulated:true`; prices use the same tier+stars demo-estimate formula as the
Gemini path, never a live quote. **In this public sample repo, `catalog_supplement.json`
ships as an empty `[]`** — like `poi_catalog.json` and `catalog.json`, the curated payload
is one of the files listed in the README's "What's not here" table; this section
describes the sourcing methodology, not data present in this export.

---

## 2. Honesty disclosures (mandatory — do not remove)

These are the load-bearing "what this is / is not" statements for the submission:

- **Settlement is SIMULATED by default.** Payment settlement runs in a sandbox
  by default — no money moves — unless the caller opts into the real Circle
  USDC rail below.
- **A real settlement rail also exists (Circle Agentic Economy Prize).** An
  explicit opt-in (`settlement_rail: "circle_usdc"`, or the web UI's "Settle
  with real USDC (testnet)" toggle) triggers a genuine Circle
  Developer-Controlled Wallets USDC transfer on Ethereum Sepolia testnet —
  real on-chain funds move. See `ucp-merchant/README.md` § *Circle Agentic
  Economy Prize integration*. This is opt-in only and independent of AP2/Alipay
  below.
- **AP2 = mandate protocol + simulated settle (Alipay rail only).** We implement
  the AP2 mandate layer (W3C Verifiable Credentials, two-tier mandates), but
  the **Alipay** settlement leg specifically is **simulated**. This is **NOT a
  full-AP2-compliant** implementation — there is no live Alipay settlement rail
  (Alipay real-rail is KIV, pending a business account).
- **NHI = long-lived on-disk signing keys.** Non-Human Identity uses long-lived signing
  keys stored on disk (RFC 9421 request signing). There is **no SPIFFE / OIDC / DID /
  RFC 8693 token exchange yet** — those are acknowledged gaps, not shipped.
- **Data is SEEDED (January 2026 snapshot).** The catalog and reference layers are a
  point-in-time seed, not a live feed. The one genuinely live source is GDACS
  (active-hazard overlay). City coverage is **BETA** — depth varies by city.
- **International airfare is NOT in the enforced budget.** The enforced/bookable budget
  covers lodging + insurance + visa/entry fees + entry-required vaccines. International
  airfare is out of scope of the enforced total (no OTA/GDS access) — it is a stated
  "bookable package, not trip total" gap.

---

## 3. Repository attribution (required notices)

The following credits satisfy the repo-level attribution requirements of the
licenses above; they are reproduced here and in the public README.

> City coordinates & population from the **SimpleMaps World Cities Database**
> (https://simplemaps.com/data/world-cities), licensed under **CC BY 4.0**.

> Map / POI geometry & points of interest © **OpenStreetMap contributors**,
> available under the **Open Database License (ODbL 1.0)** —
> https://www.openstreetmap.org/copyright

> Notability & reference links derived from **Wikidata** (**CC0**) and
> **Wikipedia** (article text **CC BY-SA**).

> Disaster / climate overlays credit **GDACS**, **Copernicus Emergency Management
> Service**, **NOAA** (CPC/NHC, US Gov public domain), and **CDC Travelers’
> Health** (US Gov public domain).

Third-party *library* notices (Python / Go / npm dependencies) are governed by
their respective licenses; run `pip-licenses`, `go-licenses`, and an npm license
sweep to regenerate the full dependency manifest.
