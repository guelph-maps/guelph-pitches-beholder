"""Pull Guelph's Park Courts and Sports Fields into this dataset's feature store.

    python fetch.py            # refresh data/source.db
    python fetch.py --print    # and show the distinct-value crosstab

This is the dataset's L1, and the only place that knows Guelph's vocabulary.
`TENNIS`, `SR. SOFTBALL`, `NATURAL TURF`, `Lights=UNKNOWN` are one city's field
values on one set of dates; the engine's pitches pack reads OSM vocabulary only
(`sport=softball`, `surface=grass`, no `lit` tag at all). The translation
happens here, once, at ingest, against the tables in
`guelph-osm-import-audit/findings/parks-recreation.md`, which derived them from
`returnDistinctValues` on the live service rather than by guessing.

Three caveats from that audit are carried as `flags` rather than silently
resolved, because each one is a judgement a mapper should make with imagery:

* `wicket_strip` — the 3 CRICKET polygons are 76/102/105 m², the artificial
  wicket strip, not the ground. `leisure=pitch`+`sport=cricket` in OSM means the
  whole oval, so these rows locate a ground they do not describe.
* `survey_multipurpose` — 3 rows say `Type=MULTI-PURPOSE` while their own
  CourtName says BASKETBALL. Tagged basketball, flagged for survey.
* `single_hoop_pad` — 5 basketball rows are under 40 m², a single-hoop pad
  rather than a court.

and the 2 DISC GOLF rows are 29,156 and 88,922 m² whole courses, so they are
ingested as `leisure=disc_golf_course`, not as pitches.

## Why this file rather than address-vault's ArcGIS fetcher

`addressvault/fetch/arcgis.py` *can* be pointed at an arbitrary layer: `Source`
is a plain dataclass, its validation only runs in `from_toml`, both layers
support `f=geojson`, and `has_coords` accepts polygons. The cost is installing
`address-vault` into the beholder's venv for 191 rows — pulling in its Windows
link-cost probe and its dated-snapshot layout — and still writing the
geojson -> centroid -> sqlite half by hand. Doc 13 asked the question; the
answer is "yes, but not worth it here", and no change was made to the vault.
One page, two layers, no pagination: this file is the whole of it.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
# The engine holds the store's schema: it is the contract between this fetcher
# and the pack that reads it, and only one of the two may own it.
sys.path.insert(0, str(HERE.parent / "address-beholder"))
from beholder.packs.pitches import SOURCE_SCHEMA  # noqa: E402

BASE = "https://gismaps.guelph.ca/hosting/rest/services/OpenData/OpenData1/FeatureServer"
LAYERS = {"Park Courts": 22, "Sports Fields": 23}
QUERY = {"f": "geojson", "where": "1=1", "outFields": "*", "outSR": 4326,
         "returnGeometry": "true"}
# The host was expected to serve an incomplete certificate chain (curl -k /
# verify=False). It does not, as of 2026-09-12: plain curl and requests both
# verify fine. Left verifying, rather than disabling a check that turned out to
# be unnecessary.
VERIFY = True
TIMEOUT = 120

DB_PATH = HERE / "data" / "source.db"

# --- Guelph's vocabulary -> OSM's -------------------------------------------

#: Park Courts `Type`. MULTI-PURPOSE is tagged from its own CourtName, which
#: says BASKETBALL on all three rows, and flagged rather than invented as
#: `sport=multi`.
COURT_SPORT = {
    "TENNIS": "tennis",
    "BASKETBALL": "basketball",
    "VOLLEYBALL": "volleyball",     # overridden to beachvolleyball on sand
    "MULTI-PURPOSE": "basketball",
}

#: Sports Fields `Type`. JR./SR. is a size grading, not a sport, so it
#: collapses. FOOTBALL is ambiguous in Canada (the three polygons sit on the
#: American figure, but Ontario high schools play both codes) — no sport rather
#: than a guess. DISC GOLF is not a pitch at all.
FIELD_SPORT = {
    "MINI SOCCER": "soccer",
    "JR. SOCCER": "soccer",
    "SR. SOCCER": "soccer",
    "JR. SOFTBALL": "softball",
    "SR. SOFTBALL": "softball",
    "HARDBALL": "baseball",
    "BASEBALL": "baseball",
    "LAWN BOWLING": "bowls",
    "CRICKET": "cricket",
    "FOOTBALL": "",
    "DISC GOLF": "disc_golf",
}

SURFACE = {
    "ASPHALT": "asphalt",
    "NATURAL TURF": "grass",
    "GRASS": "grass",
    "ARTIFICIAL TURF": "artificial_turf",
    "SAND": "sand",
    "UNKNOWN": "",
}

#: `Lights=UNKNOWN` must produce no `lit` tag, not `lit=no` — 10 courts and 8
#: fields, where the city does not know.
LIT = {"YES": "yes", "NO": "no", "UNKNOWN": ""}

OPERATOR = {
    "CITY OF GUELPH": "City of Guelph",
    "UGDSB": "Upper Grand DSB",
    "WCDSB": "Wellington Catholic DSB",
    "OTHER": "",
}

SINGLE_HOOP_MAX_M2 = 40.0


def feature_id(source_id: str) -> int:
    """A stable integer point id from the GlobalID.

    Not OBJECTID: both layers number it from 1 (so the two would collide), and a
    hosted ArcGIS layer has been seen renumbering OBJECTID on every pull. The
    beholder's history is append-only and keyed on this number, so it has to
    mean the same feature next year.
    """
    return int(hashlib.sha1(source_id.encode()).hexdigest()[:15], 16)


def _clean(value) -> str:
    return "" if value is None else str(value).strip()


def _rings(geometry: dict) -> list[list[list[float]]]:
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        return [coords[0]] if coords else []
    if kind == "MultiPolygon":
        return [p[0] for p in coords if p]
    return []


def centroid(geometry: dict) -> tuple[float, float]:
    """Area-weighted centroid of the outer rings, in degrees.

    The shoelace centroid, not the mean vertex: an L-shaped or unevenly noded
    polygon puts the mean vertex off the field, and the centroid is what the
    predicate compares against OSM's `center`.
    """
    sx = sy = sa = 0.0
    for ring in _rings(geometry):
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            cross = x1 * y2 - x2 * y1
            sa += cross
            sx += (x1 + x2) * cross
            sy += (y1 + y2) * cross
    if sa:
        return sy / (3 * sa), sx / (3 * sa)
    pts = [p for ring in _rings(geometry) for p in ring]
    if not pts:
        raise ValueError("feature has no geometry")
    return (sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts))


def translate(layer: str, props: dict, area: float) -> dict:
    """One source row in OSM vocabulary, plus the caveats it carries."""
    flags: list[str] = []
    type_ = _clean(props.get("Type")).upper()
    leisure = "pitch"

    if layer == "Park Courts":
        name = _clean(props.get("CourtName"))
        surface = SURFACE.get(_clean(props.get("Surface")).upper(), "")
        sport = COURT_SPORT.get(type_, "")
        if sport == "volleyball" and surface == "sand":
            # All 13 are sand, and OSM already holds 8 beachvolleyball + 5
            # volleyball for them — the 5 are arguably mis-tagged today.
            sport = "beachvolleyball"
        if type_ == "MULTI-PURPOSE":
            flags.append("survey_multipurpose")
        if sport == "basketball" and area and area < SINGLE_HOOP_MAX_M2:
            flags.append("single_hoop_pad")
        if sport == "basketball" and surface == "grass":
            flags.append("implausible_surface")
    else:
        name = _clean(props.get("FieldName"))
        # Surface2 is the infield (gravel/clay/limestone on the diamonds). OSM
        # has no settled two-surface scheme for a ball diamond, so it is kept in
        # `props` and never concatenated into `surface`.
        surface = SURFACE.get(_clean(props.get("Surface1")).upper(), "")
        sport = FIELD_SPORT.get(type_, "")
        if type_ == "DISC GOLF":
            leisure = "disc_golf_course"
        if type_ == "CRICKET":
            flags.append("wicket_strip")
        if type_ == "FOOTBALL":
            flags.append("football_code_unknown")

    if not sport and type_ not in ("FOOTBALL",):
        flags.append("unmapped_type")
    return {
        "name": name,
        "leisure": leisure,
        "sport": sport,
        "surface": surface,
        "lit": LIT.get(_clean(props.get("Lights")).upper(), ""),
        "operator": OPERATOR.get(_clean(props.get("Ownership")).upper(), ""),
        "flags": ",".join(flags),
    }


def fetch_layer(layer: str, number: int) -> list[dict]:
    url = f"{BASE}/{number}/query"
    resp = requests.get(url, params=QUERY, timeout=TIMEOUT, verify=VERIFY)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:  # ArcGIS serves error bodies under HTTP 200
        raise SystemExit(f"{layer}: {body['error']}")
    feats = body.get("features") or []
    if not feats:
        raise SystemExit(f"{layer}: no features — refusing to write an empty store")
    return feats


def store(rows: list[tuple]) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SOURCE_SCHEMA)
        # first_seen survives a re-pull; last_seen moves. A row that stops being
        # published keeps its history rather than vanishing from it.
        conn.executemany(
            """
            INSERT INTO features (source_id, feature_id, layer, name, leisure,
                sport, surface, lit, operator, area_m2, lat, lon, geometry,
                flags, props, first_seen, last_seen)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET
                layer=excluded.layer, name=excluded.name, leisure=excluded.leisure,
                sport=excluded.sport, surface=excluded.surface, lit=excluded.lit,
                operator=excluded.operator, area_m2=excluded.area_m2,
                lat=excluded.lat, lon=excluded.lon, geometry=excluded.geometry,
                flags=excluded.flags, props=excluded.props,
                last_seen=excluded.last_seen
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--print", dest="show", action="store_true",
                   help="print the Type/sport crosstab of what was pulled")
    args = p.parse_args(argv)

    today = date.today().isoformat()
    rows: list[tuple] = []
    tally: collections.Counter = collections.Counter()
    for layer, number in LAYERS.items():
        feats = fetch_layer(layer, number)
        print(f"  {layer}: {len(feats)} features")
        for f in feats:
            props = f.get("properties") or {}
            geom = f.get("geometry") or {}
            source_id = _clean(props.get("GlobalID")) or f"{layer}/{props.get('OBJECTID')}"
            area = float(props.get("Shape__Area") or 0.0)  # m²: the service is UTM 17N
            lat, lon = centroid(geom)
            t = translate(layer, props, area)
            tally[(layer, _clean(props.get("Type")), t["sport"], t["leisure"])] += 1
            rows.append((
                source_id, feature_id(source_id), layer, t["name"], t["leisure"],
                t["sport"], t["surface"], t["lit"], t["operator"], area, lat, lon,
                json.dumps(geom, separators=(",", ":")), t["flags"],
                json.dumps(props, separators=(",", ":")), today, today,
            ))
    store(rows)
    print(f"Stored {len(rows)} features in {DB_PATH}")
    if args.show:
        for (layer, type_, sport, leisure), n in sorted(tally.items()):
            print(f"  {layer:14} {type_:15} -> {leisure}/{sport or '(no sport)':16} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
