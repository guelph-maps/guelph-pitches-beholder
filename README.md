# Guelph Pitches Beholder

The **Guelph pitches dataset** for [`address-beholder`](../address-beholder):
audits how completely and how correctly the City of Guelph's park courts and
sports fields are represented in OpenStreetMap, over time.
Part of the [guelph-maps](https://github.com/guelph-maps) organisation, which
indexes every Guelph project.

It is the first dataset in the family that is not addresses, and the exemplar
for the engine's domain-pack seam (`address-importer-friend/future-work/
multi-city/13-guelph-feature-beholders.md`). Justified by
[`guelph-osm-import-audit`](https://github.com/guelph-maps/guelph-osm-import-audit)
(private) `findings/parks-recreation.md`, which scored both layers
**tier 4, conflate**: OSM already holds more pitches than the City publishes, so
there is nothing to import — what is missing is *tagging*. The park polygons
these courts and fields sit in are the same City layer behind
[`guelph-parks-layer`](https://github.com/guelph-maps/guelph-parks-layer), the
reference tile layer, [live here](https://guelph-maps.github.io/guelph-parks-layer/).

This repo holds no engine code. It is a dataset directory — `config.toml`,
`fetch.py`, and a gitignored `data/` — run by the engine beside it:

```sh
.\..\address-beholder\.venv\Scripts\python.exe fetch.py      # refresh the city layers
cd ../address-beholder
.\.venv\Scripts\python.exe run.py --dataset-dir ../guelph-pitches-beholder review
.\.venv\Scripts\python.exe run.py --dataset-dir ../guelph-pitches-beholder serve
```

`--cache` reuses the last Overpass extract. A full review takes about a second.

## The identity predicate: proximity + compatible sport

**Not name.** OSM carries `name` on 15 of Guelph's 303 pitches while the City
names all 191 of its own, and those missing names are the product of this
dataset — keying on name would report every unnamed OSM pitch as a *missing
feature* and hide the exact thing worth flagging. The key is where the pitch is
and what is played on it; `name`, `surface` and `lit` are what gets audited.
That is the general rule: the key must never be the thing you are auditing.

A pitch is PRESENT when an OSM element with the same `leisure` value and a
compatible `sport` lies inside its polygon (`inside`) or within 25 m of the
polygon's **edge** (`nearby`), or when one of the two sides does not say what
sport it is (`untyped`, ranked last so a tagged element always wins).

Doc 13 sketched this as `proximity+type`; the implemented predicate is declared
as `proximity+sport`, since `sport=` is the tag it actually compares. Measuring
to the polygon edge rather than between centroids is what lets one radius serve
a 33 m² hoop pad and an 89,000 m² disc golf course — 20, 25, 30 and 40 m all
return the same verdict on all 191 rows, so nothing here is tuned.

## Where it stood on 2026-09-12 (review #1)

191 city rows against 331 OSM `leisure=pitch` / `disc_golf_course` elements in
the Guelph bbox. (The audit's 303 counted the admin boundary; the bbox is a
rectangle and catches township pitches in the corners.)

| | rows | missing |
|---|--:|--:|
| Sports Fields (OD1/23) | 123 | 8 |
| Park Courts (OD1/22) | 68 | 6 |
| **all** | **191** | **14** |

**177 PRESENT, 14 MISSING**, matched as 143 `inside`, 27 `nearby`, 7 `untyped`.
One single row of the 177 is fully clean. The rest:

| code | count | what it means |
|---|--:|---|
| `name_missing` | 162 | the headline: OSM has the pitch, not its official name |
| `surface_missing` | 130 | the City knows the surface, OSM does not say |
| `lit_missing` | 110 | likewise floodlighting |
| `shared_osm_object` | 35 | several City rows resolve to one OSM object |
| `name_differs` | 13 | both named, differently — often legitimately |
| `surface_mismatch` | 7 | a real disagreement to settle on the ground |
| `sport_missing` | 4 | OSM pitch with no `sport=` |
| `lit_mismatch` | 1 | |

By sport, missing: basketball 5 of 30, soccer 4 of 63, cricket 2 of 3, disc golf
2 of 2, beach volleyball 1 of 13; tennis, softball, baseball and bowls complete.

**The two independent confirmations.** The audit counted, by Overpass against
the admin boundary, 1 cricket pitch and 0 disc golf courses in Guelph OSM. The
predicate — which knows nothing of those counts — resolves exactly 1 of 3
cricket rows and 0 of 2 disc golf rows. That is the check that the identity
predicate is measuring what it claims to.

**`shared_osm_object` is the structural finding.** 35 City rows resolve to 13
OSM objects: OSM maps the court *block* while the City inventories the courts
inside it (one way named "Tennis 7" against the City's 7A, 7B, 7C and 7D; one
beach-volleyball way against three courts). It is not an error in either
dataset, and a mapper splitting one into four wants to be told which.

## Caveats carried from the audit

Three source traps are flagged at ingest rather than silently resolved, because
each is a judgement for a mapper with imagery:

- **`wicket_strip`** — the 3 CRICKET polygons are 76/102/105 m², the artificial
  wicket strip, not the ground. `leisure=pitch` + `sport=cricket` in OSM means
  the whole oval, so these rows locate a ground they do not describe. They
  search a wider radius for that reason.
- **`survey_multipurpose`** — 3 rows say `Type=MULTI-PURPOSE` while their own
  CourtName says BASKETBALL. Tagged basketball, flagged for survey.
- **`single_hoop_pad`** — 5 basketball rows under 40 m², a hoop pad and not a
  court. Four of the five are MISSING, which is plausible: nobody maps them.

and two more resolved in `fetch.py`: all 13 VOLLEYBALL courts are `Surface=SAND`
and so `sport=beachvolleyball`, and the 2 DISC GOLF rows are whole courses,
ingested as `leisure=disc_golf_course` rather than pitches.

`Lights=UNKNOWN` (10 courts, 8 fields) produces **no** `lit` value and therefore
no check — unknown must not become `lit=no`. Same for `Surface=UNKNOWN`.
`Surface2` (the diamond infield) is kept in `props` and never merged into
`surface`; OSM has no settled two-surface scheme for a ball diamond.

## Why `fetch.py` and not address-vault

Doc 13 asked whether `addressvault/fetch/arcgis.py` could be pointed at an
arbitrary layer URL. It can: `Source` is a plain dataclass, its validation runs
only in `from_toml`, both layers advertise `f=geojson`, and the vault's
`has_coords` accepts polygons. The cost is installing `address-vault` into the
beholder's venv for 191 rows — with its Windows link-cost probe and its dated
snapshot layout — and still hand-writing the geojson → centroid → sqlite half.
One page per layer, no pagination: this file is the whole of it, and
`address-vault` is unchanged.

The host was expected to serve an incomplete certificate chain (`curl -k`,
`verify=False`). As of 2026-09-12 it does not; the fetcher verifies normally.

## Related

- [`address-beholder`](https://github.com/skfd/address-beholder) — the engine,
  whose `beholder/packs/pitches.py` holds the predicate and the checks
- [`guelph-beholder`](https://github.com/guelph-maps/guelph-beholder) — the same
  engine on Guelph addresses
- `guelph-osm-import-audit` — the audit that scored these two layers, and the
  source of every `Type` → `sport` mapping in `fetch.py`
