import io
import json
import math
import os
import threading
import time
import zipfile

import requests
from PIL import Image
from pyproj import Geod, Transformer
from osgeo import ogr

from .flypath_engine import plan_2d, plan_oblique, plan_orbit, plan_panorama, PlanningError
from .flypath_engine.grid import find_optimal_direction, generate_grid
from .flypath_engine.measurements import route_distance_m, survey_area_m2
from .flypath_engine.panorama import AUTO_SPACING_PER_ALTITUDE
from .flypath_engine.planning import DIRECTION_CONVENTION
from .flypath_engine.precision import CANONICAL_CONTRACT_VERSION
from .flypath_engine.profiles import PROFILE_VERSION
from .flypath_engine.route import split_by_waypoint_count
from .flypath_engine.statistics import mission_statistics
from . import wpml

ogr.UseExceptions()

GEOD = Geod(ellps="WGS84")
HALT_S = 2.5
BATTERY_RESERVE = 0.30
TAKEOFF_RADIUS_M = 500.0
MAX_ORBIT_RINGS = 3
SPLIT_FIXES = {"waypoint_limit_exceeded", "battery_time_exceeded",
               "splitting_required_waypoint_limit", "splitting_required_battery_limit"}
COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
PASS_NAMES = {"nadir": "Nadir", "grid": "Grid", "cross_grid": "Cross grid"}
FINISH_ACTIONS = {"Return to Home": "return_to_home", "Hover in place": "hover", "Land at last waypoint": "land"}
RC_LOST_ACTIONS = ("Return to Home", "Hover in place", "Land immediately", "Continue mission")
KINDS = ("2d", "oblique", "corridor", "orbit", "panorama")


def _load_drones():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "flypath_engine", "profiles", "drones.json"), encoding="utf-8") as f:
        raw = json.load(f)
    out = {}
    for name, p in raw.items():
        if p.get("available") is False or p.get("category") != "consumer" or not p.get("website_code"):
            continue
        a, c = p["aircraft"], p["camera"]
        out[p["website_code"]] = {
            "code": p["website_code"], "name": name, "info": p.get("info", ""),
            "sw": c["sensor_width_mm"], "sh": c["sensor_height_mm"], "f": c["focal_length_mm"],
            "iw": c["image_width_px"], "ih": c["image_height_px"], "interval": c["min_shoot_interval_s"],
            "min_speed": a["min_speed_ms"], "max_speed": a["max_speed_ms"],
            "battery": a["battery_time_min"], "battery_safe": a["battery_safe_min"],
            "enum": a["drone_enum"], "sub_enum": a.get("drone_sub_enum", 0),
        }
    return out


DRONES = _load_drones()

DEFAULTS = {
    "kind": "2d", "capture": "full", "path": "curved", "drone": next(iter(DRONES)),
    "altitude": 100.0, "side": 70, "front": 70, "speed": 5.0, "direction": 0.0, "auto_direction": False,
    "margin": 0.0, "oblique_pattern": "fast", "oblique_pitch": -45, "oblique_nadir": True,
    "buffer": 10.0, "rings": [[30.0, 100.0, -35]], "panorama_layout": "points", "panorama_overlap": 30,
    "panorama_nadir": True, "panorama_spacing": 0, "split_enabled": True, "split_count": 1,
    "split_overridden": False, "max_wp": 70, "cross_hatch": False, "reverse": False, "terrain": False,
    "terrain_tol": 5.0, "finish": "Return to Home", "rc_lost": "Return to Home", "takeoff_tol": 2.0,
    "launch_offset": 0.0, "same_takeoff": True,
}


class PlanError(ValueError):
    pass


def _num(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return min(hi, max(lo, v))


def clean_settings(s):
    s = s if isinstance(s, dict) else {}
    d = DEFAULTS
    out = {}
    out["kind"] = s.get("kind") if s.get("kind") in KINDS else d["kind"]
    out["capture"] = "semi" if s.get("capture") == "semi" else "full"
    out["path"] = "straight" if s.get("path") == "straight" else "curved"
    out["drone"] = s.get("drone") if s.get("drone") in DRONES else d["drone"]
    drone = DRONES[out["drone"]]
    point = out["kind"] in ("orbit", "panorama")
    out["altitude"] = round(_num(s.get("altitude"), 10.0 if point else 30.0, 500.0, d["altitude"]), 1)
    out["side"] = int(_num(s.get("side"), 50, 95, d["side"]))
    out["front"] = int(_num(s.get("front"), 50, 95, d["front"]))
    out["speed"] = round(_num(s.get("speed"), drone["min_speed"], drone["max_speed"], d["speed"]), 1)
    out["direction"] = round(_num(s.get("direction"), 0.0, 359.9, 0.0), 1)
    out["auto_direction"] = bool(s.get("auto_direction"))
    out["margin"] = round(_num(s.get("margin"), 0.0, 200.0, 0.0), 1)
    out["oblique_pattern"] = "full" if s.get("oblique_pattern") == "full" else "fast"
    out["oblique_pitch"] = int(_num(s.get("oblique_pitch"), -85, -20, -45))
    out["oblique_nadir"] = s.get("oblique_nadir", True) is not False
    out["buffer"] = round(_num(s.get("buffer"), 1.0, 500.0, 10.0), 1)
    rings = []
    for r in (s.get("rings") or d["rings"])[:MAX_ORBIT_RINGS]:
        try:
            rings.append([round(_num(r[0], 5.0, 2000.0, 30.0), 1), round(_num(r[1], 10.0, 500.0, 100.0), 1), int(_num(r[2], -90, 0, -35))])
        except (TypeError, IndexError):
            continue
    out["rings"] = rings or [list(x) for x in d["rings"]]
    if out["kind"] == "orbit":
        out["altitude"] = out["rings"][0][1]
    out["panorama_layout"] = "area" if s.get("panorama_layout") == "area" else "points"
    out["panorama_overlap"] = int(_num(s.get("panorama_overlap"), 20, 70, 30))
    out["panorama_nadir"] = s.get("panorama_nadir", True) is not False
    out["panorama_spacing"] = int(_num(s.get("panorama_spacing"), 0, 5000, 0))
    out["split_enabled"] = s.get("split_enabled", True) is not False
    out["split_count"] = int(_num(s.get("split_count"), 1, 999, 1))
    out["split_overridden"] = bool(s.get("split_overridden"))
    out["max_wp"] = int(_num(s.get("max_wp"), 2, 200, 70))
    out["cross_hatch"] = bool(s.get("cross_hatch"))
    out["reverse"] = bool(s.get("reverse"))
    out["terrain"] = bool(s.get("terrain")) and not point
    out["terrain_tol"] = float(round(_num(s.get("terrain_tol"), 1.0, 100.0, 5.0)))
    out["finish"] = s.get("finish") if s.get("finish") in FINISH_ACTIONS else d["finish"]
    out["rc_lost"] = s.get("rc_lost") if s.get("rc_lost") in RC_LOST_ACTIONS else d["rc_lost"]
    out["takeoff_tol"] = round(_num(s.get("takeoff_tol"), 0.1, 50.0, 2.0), 1)
    out["launch_offset"] = round(_num(s.get("launch_offset"), -50.0, 50.0, 0.0), 1)
    out["same_takeoff"] = s.get("same_takeoff", True) is not False
    if out["kind"] == "panorama":
        out["capture"] = "full"
    if out["kind"] == "orbit":
        out["path"] = "curved"
    if out["terrain"] and out["kind"] == "2d":
        out["reverse"] = False
    return out


def _pt(p):
    lon, lat = float(p[0]), float(p[1])
    if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
        raise PlanError("A point is outside the map.")
    return lon, lat


def _polygons(g):
    if not isinstance(g, dict):
        return []
    t = g.get("type")
    if t == "Polygon":
        return [g["coordinates"]]
    if t == "MultiPolygon":
        return list(g["coordinates"])
    return []


def _lines(g):
    if not isinstance(g, dict):
        return []
    t = g.get("type")
    if t == "LineString":
        return [g["coordinates"]]
    if t == "MultiLineString":
        return list(g["coordinates"])
    return []


def _ring_points(ring):
    pts = [_pt(p) for p in ring]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def survey_area(geom):
    parts = []
    for poly in _polygons(geom):
        if not poly:
            continue
        ext = _ring_points(poly[0])
        if len(ext) < 3:
            continue
        parts.append({
            "exterior": [{"latitude_deg": lat, "longitude_deg": lon} for lon, lat in ext],
            "holes": [[{"latitude_deg": lat, "longitude_deg": lon} for lon, lat in _ring_points(h)] for h in poly[1:] if len(_ring_points(h)) >= 3],
        })
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else {"parts": parts}


_transformers = {}
_tlock = threading.Lock()


def utm_epsg(lon, lat):
    zone = min(60, max(1, int((lon + 180.0) / 6.0) + 1))
    return (32600 if lat >= 0 else 32700) + zone


def transformers(epsg):
    with _tlock:
        t = _transformers.get(epsg)
        if t is None:
            t = (Transformer.from_crs("EPSG:4326", "EPSG:%d" % epsg, always_xy=True),
                 Transformer.from_crs("EPSG:%d" % epsg, "EPSG:4326", always_xy=True))
            _transformers[epsg] = t
        return t


TERRARIUM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
ZOOM = 12
TILE_SIZE = 256
_DENSIFY_MAX_SAMPLES = 1500
_DEFAULT_MAX_WAYPOINTS = 900


class TerrainError(Exception):
    pass


def tile_coords(lat, lon, zoom=ZOOM):
    lat = max(-85.0511, min(85.0511, float(lat)))
    lon = max(-180.0, min(180.0, float(lon)))
    n = 2 ** zoom
    xf = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    yf = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    x, y = int(xf), int(yf)
    px = min(TILE_SIZE - 1, int((xf - x) * TILE_SIZE))
    py = min(TILE_SIZE - 1, int((yf - y) * TILE_SIZE))
    return x, y, px, py


def elevation_from_rgb(r, g, b):
    return (r * 256 + g + b / 256.0) - 32768.0


class TerrainSampler:
    def __init__(self):
        self._tiles = {}
        self._failed = {}
        self._lock = threading.Lock()

    def clear(self):
        with self._lock:
            self._tiles.clear()

    def sample(self, lon, lat):
        x, y, px, py = tile_coords(lat, lon, ZOOM)
        key = (x, y)
        with self._lock:
            hit = key in self._tiles
            tile = self._tiles.get(key)
            if hit and tile is None and time.time() - self._failed.get(key, 0) > 15:
                del self._tiles[key]
                hit = False
        if hit:
            if tile is None:
                raise TerrainError("Elevation data is currently unavailable.")
        else:
            try:
                tile = self._fetch(x, y)
            except TerrainError:
                with self._lock:
                    if len(self._tiles) < 256:
                        self._tiles[key] = None
                        self._failed[key] = time.time()
                raise
            with self._lock:
                if len(self._tiles) >= 64:
                    self._tiles.clear()
                self._tiles[key] = tile
        r, g, b = tile[py * TILE_SIZE + px]
        return elevation_from_rgb(r, g, b)

    def _fetch(self, x, y):
        try:
            r = requests.get(TERRARIUM_URL.format(z=ZOOM, x=x, y=y), timeout=10, headers={"User-Agent": "WebODM Mission Planner"})
        except requests.RequestException:
            raise TerrainError("Elevation data is currently unavailable.")
        if r.status_code != 200:
            raise TerrainError("Elevation data is currently unavailable.")
        try:
            with Image.open(io.BytesIO(r.content)) as im:
                if im.size != (TILE_SIZE, TILE_SIZE):
                    raise TerrainError("Elevation tile could not be decoded.")
                return list(im.convert("RGB").getdata())
        except TerrainError:
            raise
        except Exception:
            raise TerrainError("Elevation tile could not be decoded.")


TERRAIN = TerrainSampler()


def _haversine_m(lon1, lat1, lon2, lat2):
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    h = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(h)))


def sample_elevations(waypoints, sample):
    return [sample(lon, lat) for lon, lat in waypoints]


def densify_by_terrain(waypoints, sample, tolerance_m, spacing_m=None, max_points=_DEFAULT_MAX_WAYPOINTS,
                       all_legs=False, leg_sample=None):
    pts = list(waypoints)
    if len(pts) < 2:
        return pts, [sample(lon, lat) for lon, lat in pts]
    leg_len = [_haversine_m(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]) for i in range(len(pts) - 1)]
    strip_total = sum(length for i, length in enumerate(leg_len) if all_legs or i % 2 == 0)
    if spacing_m is None:
        spacing_m = min(30.0, max(8.0, float(tolerance_m) * 5.0))
    spacing = max(spacing_m, strip_total / max(1, _DENSIFY_MAX_SAMPLES - len(pts)))
    candidates = []
    for i, length in enumerate(leg_len):
        candidates.append((pts[i][0], pts[i][1], True, i))
        if (all_legs or i % 2 == 0) and length > spacing:
            extra = int(length // spacing)
            for j in range(1, extra + 1):
                f = j / (extra + 1)
                candidates.append((pts[i][0] + f * (pts[i + 1][0] - pts[i][0]),
                                   pts[i][1] + f * (pts[i + 1][1] - pts[i][1]), False, i))
    candidates.append((pts[-1][0], pts[-1][1], True, len(leg_len) - 1))
    if leg_sample is None:
        elevations = [sample(lon, lat) for lon, lat, _, _ in candidates]
    else:
        elevations = [leg_sample(lon, lat, leg) for lon, lat, _, leg in candidates]
    tolerance = max(0.5, float(tolerance_m))
    for _ in range(6):
        out_pts, out_elevs = [], []
        reference = None
        for (lon, lat, is_orig, _leg), elev in zip(candidates, elevations):
            if is_orig:
                out_pts.append((lon, lat))
                out_elevs.append(elev)
                reference = elev
            elif reference is None or abs(elev - reference) > tolerance:
                out_pts.append((lon, lat))
                out_elevs.append(elev)
                reference = elev
        if len(out_pts) <= max_points:
            break
        tolerance *= 2
    if len(out_pts) > max_points:
        raise ValueError("Terrain following exceeds the waypoint limit. Increase the limit or terrain tolerance, or split the flight further.")
    return out_pts, out_elevs


def heights_above_takeoff(elevations, altitude_m, base=None):
    if not elevations:
        return []
    if base is None:
        base = elevations[0]
    return [round(altitude_m + (elev - base), 1) for elev in elevations]


def _dist(a, b):
    return math.hypot(b[0] - a[0], b[1] - a[1])


def polyline_length(coords):
    return sum(_dist(coords[i], coords[i + 1]) for i in range(len(coords) - 1))


def sample_polyline(coords, spacing):
    if not coords:
        return []
    spacing = max(float(spacing), 0.5)
    clean = [coords[0]]
    for c in coords[1:]:
        if _dist(clean[-1], c) > 1e-9:
            clean.append(c)
    if len(clean) == 1:
        return [clean[0]]
    out = [clean[0]]
    for a, b in zip(clean, clean[1:]):
        seg_len = _dist(a, b)
        n = max(1, int(math.ceil(seg_len / spacing)))
        for k in range(1, n + 1):
            t = k / n
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return out


def compute_pass_offsets(half_width_m, line_spacing_m, footprint_across_m):
    half_width_m = max(float(half_width_m), 0.0)
    line_spacing_m = max(float(line_spacing_m), 0.5)
    footprint_across_m = max(float(footprint_across_m), 0.0)
    total = 2.0 * half_width_m
    if total <= footprint_across_m + 1e-9 or total <= 1e-9:
        return [0.0]
    n_extra = math.ceil((total - footprint_across_m) / line_spacing_m)
    n_passes = n_extra + 1
    centre = (n_passes - 1) / 2.0
    return [(i - centre) * line_spacing_m for i in range(n_passes)]


def snake_passes(passes):
    route = []
    for i, pts in enumerate(passes):
        route.extend(list(reversed(pts)) if i % 2 else list(pts))
    return route


def _line_x(p1, d1, p2, d2):
    den = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(den) < 1e-12:
        return None
    t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / den
    return p1[0] + d1[0] * t, p1[1] + d1[1] * t


def offset_coords(coords, distance, mitre_limit=2.0):
    pts = [coords[0]]
    for c in coords[1:]:
        if _dist(pts[-1], c) > 1e-9:
            pts.append(c)
    if len(pts) < 2:
        return None
    segs = []
    for a, b in zip(pts, pts[1:]):
        L = _dist(a, b)
        d = ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
        n = (-d[1], d[0])
        segs.append(((a[0] + n[0] * distance, a[1] + n[1] * distance), (b[0] + n[0] * distance, b[1] + n[1] * distance), d))
    out = [segs[0][0]]
    for i in range(len(segs) - 1):
        a0, a1, da = segs[i]
        b0, b1, db = segs[i + 1]
        cross = da[0] * db[1] - da[1] * db[0]
        dot = da[0] * db[0] + da[1] * db[1]
        if abs(cross) < 1e-9 and dot > 0:
            out.append(a1)
            continue
        outer = cross * distance < 0
        x = _line_x(a0, da, b0, db)
        if not outer:
            if x is not None:
                out.append(x)
            continue
        v = pts[i + 1]
        if x is not None and _dist(v, x) <= mitre_limit * abs(distance):
            out.append(x)
            continue
        bis = (a1[0] + b0[0] - 2 * v[0], a1[1] + b0[1] - 2 * v[1])
        bl = math.hypot(*bis)
        if bl < 1e-12:
            out.extend([a1, b0])
            continue
        bis = (bis[0] / bl, bis[1] / bl)
        c = (v[0] + bis[0] * mitre_limit * abs(distance), v[1] + bis[1] * mitre_limit * abs(distance))
        perp = (-bis[1], bis[0])
        p = _line_x(c, perp, a0, da)
        q = _line_x(c, perp, b0, db)
        out.extend([p or a1, q or b0])
    out.append(segs[-1][1])
    clean = [out[0]]
    for p in out[1:]:
        if _dist(clean[-1], p) > 1e-9:
            clean.append(p)
    changed = True
    while changed and len(clean) > 2:
        changed = False
        for i in range(len(clean) - 1):
            a, b = clean[i], clean[i + 1]
            j = min(range(len(pts) - 1), key=lambda k: _seg_dist(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), pts[k], pts[k + 1]))
            d = (pts[j + 1][0] - pts[j][0], pts[j + 1][1] - pts[j][1])
            if (b[0] - a[0]) * d[0] + (b[1] - a[1]) * d[1] < 0:
                del clean[i + 1 if i + 1 < len(clean) - 1 else i]
                changed = True
                break
    return clean if len(clean) >= 2 else None


def _seg_dist(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def line_parts(geom):
    parts = []
    for line in _lines(geom):
        pts = [_pt(p) for p in line]
        if len(pts) >= 2:
            parts.append(pts)
    return parts


def _line_centroid(parts):
    g = ogr.Geometry(ogr.wkbMultiLineString)
    for part in parts:
        line = ogr.Geometry(ogr.wkbLineString)
        for lon, lat in part:
            line.AddPoint_2D(lon, lat)
        g.AddGeometry(line)
    c = g.Centroid()
    return c.GetX(), c.GetY()


def generate_corridor_route(parts, altitude_m, shot_spacing_m, side_overlap, buffer_m, drone, densify_spacing=None):
    if altitude_m <= 0:
        raise ValueError("Altitude must be greater than 0.")
    if not 0.0 <= side_overlap < 1.0:
        raise ValueError("Side overlap must be between 0% and 99%.")
    if not parts:
        raise ValueError("Draw a corridor centre line first.")
    clon, clat = _line_centroid(parts)
    fwd, inv = transformers(utm_epsg(clon, clat))
    footprint_across = altitude_m * drone["sw"] / drone["f"]
    line_spacing = max(footprint_across * (1.0 - side_overlap), 0.5)
    sample_spacing = max(densify_spacing or shot_spacing_m, 0.5)
    full_auto = densify_spacing is not None
    offsets = compute_pass_offsets(buffer_m, line_spacing, footprint_across)
    waypoints = []
    for part in parts:
        xs, ys = fwd.transform([p[0] for p in part], [p[1] for p in part])
        coords = list(zip(xs, ys))
        if len(coords) < 2:
            continue
        passes = []
        for off in offsets:
            c = coords if abs(off) < 1e-9 else offset_coords(coords, off)
            if c and len(c) >= 2:
                passes.append(sample_polyline(c, sample_spacing) if full_auto else list(c))
        route = snake_passes(passes)
        if route:
            lons, lats = inv.transform([p[0] for p in route], [p[1] for p in route])
            waypoints.extend(zip(lons, lats))
    if not waypoints:
        raise ValueError("The corridor produced no waypoints. Check that the line has length and the buffer is greater than 0.")
    return [tuple(w) for w in waypoints], sample_spacing


def corridor_band(parts, buffer_m):
    if not parts:
        return None
    clon, clat = _line_centroid(parts)
    fwd, inv = transformers(utm_epsg(clon, clat))
    g = ogr.Geometry(ogr.wkbMultiLineString)
    for part in parts:
        xs, ys = fwd.transform([p[0] for p in part], [p[1] for p in part])
        line = ogr.Geometry(ogr.wkbLineString)
        for x, y in zip(xs, ys):
            line.AddPoint_2D(x, y)
        g.AddGeometry(line)
    band = g.Buffer(buffer_m, ["ENDCAP_STYLE=ROUND", "JOIN_STYLE=MITRE", "MITRE_LIMIT=2", "QUADRANT_SEGMENTS=8"])
    return _to_geojson(band, inv)


def _to_geojson(g, inv):
    def ring(r):
        pts = r.GetPoints() or []
        if not pts:
            return []
        lons, lats = inv.transform([p[0] for p in pts], [p[1] for p in pts])
        return [[round(a, 8), round(b, 8)] for a, b in zip(lons, lats)]

    def poly(p):
        return [ring(p.GetGeometryRef(i)) for i in range(p.GetGeometryCount())]

    t = ogr.GT_Flatten(g.GetGeometryType())
    if t == ogr.wkbPolygon:
        return {"type": "Polygon", "coordinates": poly(g)}
    if t in (ogr.wkbMultiPolygon, ogr.wkbGeometryCollection):
        polys = []
        for i in range(g.GetGeometryCount()):
            sub = g.GetGeometryRef(i)
            st = ogr.GT_Flatten(sub.GetGeometryType())
            if st == ogr.wkbPolygon:
                polys.append(poly(sub))
            elif st == ogr.wkbMultiPolygon:
                polys.extend(poly(sub.GetGeometryRef(j)) for j in range(sub.GetGeometryCount()))
        return {"type": "MultiPolygon", "coordinates": polys} if polys else None
    return None


def gsd_cm(drone, altitude):
    return altitude * drone["sw"] * 100.0 / (drone["f"] * drone["iw"])


def footprint(drone, altitude):
    return altitude * drone["sw"] / drone["f"], altitude * drone["sh"] / drone["f"]


def full_auto_spacing(s, drone):
    _, along = footprint(drone, s["altitude"])
    return max(along * (1.0 - s["front"] / 100.0), 0.5)


def front_overlap_fraction(s, drone):
    if s["capture"] == "full":
        return max(0.0, min(0.99, s["front"] / 100.0))
    _, along = footprint(drone, s["altitude"])
    if not along or s["speed"] <= 0 or drone["interval"] <= 0:
        return 0.7
    return max(0.0, min(0.99, 1.0 - (s["speed"] * drone["interval"]) / along))


def semi_front_overlap_pct(s, drone):
    _, along = footprint(drone, s["altitude"])
    return (1.0 - s["speed"] * drone["interval"] / along) * 100.0


def _split_value(s, max_split, default_target):
    max_split = max(1, max_split)
    target = s["split_count"]
    if not s["split_enabled"]:
        target = 1
    elif not s["split_overridden"]:
        target = default_target
    return max(1, min(target, max_split)), max_split


def _grid_request(s, area, drone):
    capture = {"mode": "full_auto" if s["capture"] == "full" else "semi_auto"}
    if capture["mode"] == "full_auto":
        capture["front_overlap_ratio"] = s["front"] / 100.0
    return {
        "contract_version": CANONICAL_CONTRACT_VERSION, "operation": "plan_2d", "mapping_style": "2d",
        "survey_area": area, "drone_profile_id": drone["code"], "profile_version": PROFILE_VERSION,
        "altitude_m": s["altitude"], "speed_m_s": s["speed"], "side_overlap_ratio": s["side"] / 100.0,
        "margin_m": s["margin"],
        "direction": {"mode": "automatic" if s["auto_direction"] else "manual", "convention": DIRECTION_CONVENTION, "value_deg": s["direction"]},
        "capture": capture, "turn_style": s["path"], "finish_action": FINISH_ACTIONS[s["finish"]],
        "split": {"enabled": s["split_enabled"], "requested_flights": s["split_count"] if s["split_enabled"] else 1, "max_waypoints_per_flight": s["max_wp"]},
        "cross_hatch": s["cross_hatch"], "reverse_route": s["reverse"], "terrain_follow": False,
    }


def _orbit_request(s, centre, drone, split_enabled=False, requested=1):
    capture = {"mode": "full_auto" if s["capture"] == "full" else "semi_auto"}
    if capture["mode"] == "full_auto":
        capture["side_overlap_ratio"] = s["side"] / 100.0
    rings = s["rings"]
    if len(rings) == 1:
        shape = {"radius_m": rings[0][0], "altitude_m": rings[0][1], "gimbal_pitch_deg": rings[0][2]}
    else:
        shape = {"rings": [{"radius_m": r, "altitude_m": a, "gimbal_pitch_deg": p} for r, a, p in rings]}
    return {
        "contract_version": CANONICAL_CONTRACT_VERSION, "operation": "plan_orbit", "mapping_style": "orbit",
        "drone_profile_id": drone["code"], "profile_version": PROFILE_VERSION,
        "centre": {"latitude_deg": centre[1], "longitude_deg": centre[0]}, **shape,
        "direction": "counterclockwise" if s["reverse"] else "clockwise", "speed_m_s": s["speed"],
        "capture": capture, "finish_action": FINISH_ACTIONS[s["finish"]],
        "split": ({"enabled": True, "requested_flights": requested, "max_waypoints_per_flight": s["max_wp"]} if split_enabled
                  else {"enabled": False, "max_waypoints_per_flight": s["max_wp"]}),
        "terrain_follow": False,
    }


def _panorama_request(s, g, drone, points=None):
    if s["panorama_layout"] == "area" and points is None:
        area = survey_area(g.get("polygon"))
        if area is None:
            raise PlanError("Draw or import the survey area first.")
        place = {"survey_area": area}
        if s["panorama_spacing"]:
            place["point_spacing_m"] = s["panorama_spacing"]
    else:
        pts = points if points is not None else [_pt(p) for p in (g.get("points") or [])]
        if not pts:
            raise PlanError("Place at least one panorama point on the map.")
        place = {"points": [{"latitude_deg": lat, "longitude_deg": lon} for lon, lat in pts]}
    return {
        "contract_version": CANONICAL_CONTRACT_VERSION, "operation": "plan_panorama", "mapping_style": "panorama",
        "drone_profile_id": drone["code"], "profile_version": PROFILE_VERSION, **place,
        "altitude_m": s["altitude"], "speed_m_s": s["speed"],
        "capture": {"mode": "full_auto", "overlap_ratio": s["panorama_overlap"] / 100.0},
        "include_nadir": s["panorama_nadir"], "finish_action": FINISH_ACTIONS[s["finish"]],
        "split": {"max_waypoints_per_flight": s["max_wp"]}, "turn_style": s["path"], "terrain_follow": False,
    }


def _pitch_per_waypoint(actions, start, end):
    rotations = {a["waypoint_index"]: float(a["pitch_deg"]) for a in actions if a["type"] == "rotate_camera"}
    pitches, current = [], None
    for index in range(start, end + 1):
        current = rotations.get(index, current)
        pitches.append(current)
    return pitches


def consume(request, result):
    oblique = request["operation"] == "plan_oblique"
    panorama = request["operation"] == "plan_panorama"
    rows = result["route"]["waypoints"]
    points = [(r["position"]["longitude_deg"], r["position"]["latitude_deg"]) for r in rows]
    raw_headings = [r.get("heading_deg") for r in rows]
    headings = [None if v is None else float(v) for v in raw_headings] if any(v is not None for v in raw_headings) else None
    raw_alt = [r.get("altitude_m") for r in rows]
    altitudes = [float(v) for v in raw_alt] if any(v is not None for v in raw_alt) else None
    out = []
    for f in result["flights"]:
        start, end = f["route_start_waypoint_index"], f["route_end_waypoint_index"]
        actions = f["actions"]
        fh = headings[start:end + 1] if headings else None
        if fh and all(v is None for v in fh):
            fh = None
        out.append({
            "waypoints": points[start:end + 1],
            "actions": [dict(a, waypoint_index=a["waypoint_index"] - start) for a in actions],
            "headings": fh,
            "heights": altitudes[start:end + 1] if altitudes else None,
            "pitches": _pitch_per_waypoint(actions, start, end) if (altitudes or oblique or panorama) else None,
            "flight": f,
        })
    return out


def _flight_names(result, count):
    op = result.get("operation")
    rows = result["route"]["waypoints"]
    names = []
    if op == "plan_panorama":
        for f in result["flights"]:
            first = rows[f["route_start_waypoint_index"]]["point_index"] + 1
            last = rows[f["route_end_waypoint_index"]]["point_index"] + 1
            names.append("point %d" % first if first == last else "points %d to %d" % (first, last))
        return names if len(names) == count else None
    if op != "plan_oblique":
        return None
    passes = result["oblique"]["passes"]
    for f in result["flights"]:
        sp = passes[rows[f["route_start_waypoint_index"]]["pass_index"]]
        names.append(PASS_NAMES.get(sp["kind"]) or "Facing %s" % COMPASS[round((sp["heading_deg"] % 360) / 45) % 8])
    return names if len(names) == count else None


def _oblique_terrain(s, result, sample):
    rows = result["route"]["waypoints"]
    passes = result["oblique"]["passes"]
    offset = result["oblique"]["offset_m"]
    full = s["capture"] == "full"
    altitude = s["altitude"]
    flights = []
    shared = sample(rows[0]["position"]["longitude_deg"], rows[0]["position"]["latitude_deg"])
    for flight in result["flights"]:
        start, end = flight["route_start_waypoint_index"], flight["route_end_waypoint_index"]
        points = [(r["position"]["longitude_deg"], r["position"]["latitude_deg"]) for r in rows[start:end + 1]]
        sp = passes[rows[start]["pass_index"]]
        legs = [sp["heading_deg"] if sp["heading_deg"] is not None else GEOD.inv(a[0], a[1], b[0], b[1])[0]
                for a, b in zip(points, points[1:])] or [sp["heading_deg"] or 0.0]

        def ground(lon, lat, leg, sp=sp, legs=legs):
            if sp["kind"] == "nadir":
                return sample(lon, lat)
            ahead = GEOD.fwd(lon, lat, legs[min(leg, len(legs) - 1)], offset)
            return max(sample(lon, lat), sample(ahead[0], ahead[1]))

        if full:
            elevations = [ground(lon, lat, i) for i, (lon, lat) in enumerate(points)]
        else:
            points, elevations = densify_by_terrain(points, sample, s["terrain_tol"], max_points=s["max_wp"], all_legs=True, leg_sample=ground)
        base = shared if s["same_takeoff"] else sample(points[0][0], points[0][1])
        flights.append({"waypoints": points, "heights": heights_above_takeoff(elevations, altitude, base=base)})
    return flights


def _stats(**kw):
    out = {"coverage_ha": None, "flight_s": None, "distance_m": None, "photos": None, "photos_estimated": False,
           "waypoints": None, "lines": None, "batteries": None, "corridor_length_m": None}
    out.update(kw)
    return out


def _plan_shared(s, g, drone):
    kind = s["kind"]
    info = None
    if kind == "orbit":
        centre = g.get("centre")
        if not centre:
            raise PlanError("Draw the orbit circle on the map first.")
        centre = _pt(centre)
        request = _orbit_request(s, centre, drone)
        if s["split_enabled"]:
            requested = s["split_count"] if s["split_overridden"] else 1
            split = requested != 1
            if not split:
                try:
                    errors = plan_orbit(request)["validation"]["errors"]
                    split = any(e["code"] in SPLIT_FIXES for e in errors)
                except PlanningError:
                    split = False
            if split:
                request = _orbit_request(s, centre, drone, True, requested)
        result = plan_orbit(request)
    elif kind == "panorama":
        request = _panorama_request(s, g, drone)
        result = plan_panorama(request)
    else:
        area = survey_area(g.get("polygon"))
        if area is None:
            raise PlanError("Draw or import the survey area first.")
        request = _grid_request(s, area, drone)
        if kind == "oblique":
            request.pop("cross_hatch")
            request.update(operation="plan_oblique", mapping_style="oblique", gimbal_pitch_deg=s["oblique_pitch"],
                           include_nadir=s["oblique_nadir"], pattern=s["oblique_pattern"])
            result = plan_oblique(request)
        else:
            result = plan_2d(request)
    flights = consume(request, result)
    terrain_failed = False
    if kind == "oblique" and s["terrain"]:
        try:
            plans = _oblique_terrain(s, result, TERRAIN.sample)
            for f, p in zip(flights, plans):
                count = len(p["waypoints"])
                f["waypoints"] = p["waypoints"]
                f["heights"] = p["heights"]
                f["headings"] = [f["headings"][0]] * count if f["headings"] else None
                f["pitches"] = [f["pitches"][0]] * count if f["pitches"] else None
        except (TerrainError, ValueError) as e:
            terrain_failed = True
            info = str(e)
    st = result["statistics"]
    lines = str(st["strip_count"])
    if kind == "orbit":
        rings = len(result["orbit"].get("rings") or (None,))
        lines = "1 ring" if rings == 1 else "%d rings" % rings
    elif kind == "panorama":
        pts = result["panorama"]["points"]
        rows = len(pts[0]["rows"])
        lines = "%d points" % len(pts) if len(pts) > 1 else ("1 row" if rows == 1 else "%d rows" % rows)
    coverage = st["survey_area_m2"] / 10000.0 if st["survey_area_m2"] else None
    stats = _stats(coverage_ha=coverage, flight_s=st["known_estimated_seconds"], distance_m=st["known_distance_m"],
                   photos=st["photo_count"], photos_estimated=st.get("photo_count_kind") == "estimate",
                   waypoints=sum(len(f["waypoints"]) for f in flights) if kind == "oblique" and s["terrain"] and not terrain_failed else st["waypoint_count"],
                   lines=lines, batteries=st["battery_count"])
    if kind == "orbit" and s["capture"] != "full":
        stats["front_overlap_pct"] = result["capture"]["side_overlap_ratio"] * 100
    if not st.get("estimates_complete", False):
        info = info or "Launch/home travel is not included in these totals."
    errors = [e["message"] for e in result["validation"]["errors"]] if not result["validation"]["export_allowed"] else []
    if errors and kind in ("orbit", "panorama"):
        info = errors[0]
    split = None
    if kind == "orbit":
        value, mx = _split_value(s, max(1, len(result["route"]["waypoints"]) - 1), st["battery_count"])
        split = {"value": value, "max": mx}
    names = _flight_names(result, len(flights))
    out_flights = []
    for i, f in enumerate(flights):
        out_flights.append({
            "waypoints": [[round(a, 9), round(b, 9)] for a, b in f["waypoints"]],
            "heights": f["heights"], "headings": f["headings"], "pitches": f["pitches"],
            "actions": f["actions"], "label": names[i] if names else None,
        })
    overlays = {}
    if kind == "panorama":
        overlays["points"] = [[p["centre"]["longitude_deg"], p["centre"]["latitude_deg"]] for p in result["panorama"]["points"]]
    plan = {
        "flights": out_flights, "stats": stats, "info": info, "errors": errors, "split": split,
        "direction": result["resolved_direction"]["value_deg"] if s["auto_direction"] and "resolved_direction" in result else None,
        "terrain_failed": terrain_failed, "overlays": overlays, "legacy_actions": False,
    }
    return plan


def _apply_terrain(s, waypoints, all_legs=False):
    if not s["terrain"] or not waypoints:
        return waypoints, None, None
    try:
        if s["capture"] == "full":
            return waypoints, sample_elevations(waypoints, TERRAIN.sample), None
        w, e = densify_by_terrain(waypoints, TERRAIN.sample, s["terrain_tol"], all_legs=all_legs)
        return w, e, None
    except (TerrainError, ValueError) as e:
        return waypoints, None, str(e)


def _plan_2d_terrain(s, g, drone):
    area = survey_area(g.get("polygon"))
    if area is None:
        raise PlanError("Draw or import the survey area first.")
    full = s["capture"] == "full"
    camera = {"focal_length_mm": drone["f"], "sensor_width_mm": drone["sw"]}
    direction = s["direction"]
    if s["auto_direction"]:
        fw, _ = footprint(drone, s["altitude"])
        try:
            direction = find_optimal_direction(area, fw * (1.0 - s["side"] / 100.0))
        except Exception:
            direction = 0.0
    if full:
        densify = full_auto_spacing(s, drone)
        spacing = densify or 0.5
    else:
        densify = None
        spacing = max(s["speed"] * drone["interval"], 0.5)
    directions = [direction, (direction + 90.0) % 360.0] if s["cross_hatch"] else [direction]
    turn_pts, waypoints = [], []
    try:
        for d in directions:
            common = dict(altitude_m=s["altitude"], shot_spacing_m=spacing, side_overlap=s["side"] / 100.0,
                          direction_deg=d, margin_m=s["margin"], camera=camera)
            tp = [(p["longitude_deg"], p["latitude_deg"]) for p in generate_grid(area, **common)["waypoints"]]
            turn_pts.extend(tp)
            if full:
                waypoints.extend((p["longitude_deg"], p["latitude_deg"]) for p in generate_grid(area, densify_spacing=densify, **common)["waypoints"])
        if not full:
            waypoints = list(turn_pts)
    except ValueError as e:
        raise PlanError(str(e))
    if len(waypoints) < 2:
        raise PlanError("Flight grid produced no waypoints.")
    waypoints, elevations, terrain_error = _apply_terrain(s, waypoints)
    dist = route_distance_m([{"longitude_deg": a, "latitude_deg": b} for a, b in turn_pts])
    n_lines = len(turn_pts) // 2
    st = mission_statistics(route_distance_m=dist, speed_m_s=s["speed"], capture_mode="full" if full else "semi",
                            photo_interval_s=drone["interval"], battery_minutes=drone["battery_safe"],
                            waypoint_count=len(waypoints), stop_seconds=HALT_S)
    value, mx = _split_value(s, max(1, len(waypoints) - 1), st["battery_count"])
    n = value if s["split_enabled"] else 1
    missions = []
    if elevations and len(elevations) == len(waypoints):
        base = elevations[0] if s["same_takeoff"] else None
        triples = [(lon, lat, e) for (lon, lat), e in zip(waypoints, elevations)]
        for part in split_by_waypoint_count(triples, n, s["max_wp"]):
            pe = [e for _, _, e in part]
            missions.append(([(a, b) for a, b, _ in part], heights_above_takeoff(pe, s["altitude"], base=base)))
    else:
        for part in split_by_waypoint_count(waypoints, n, s["max_wp"]):
            missions.append((part, None))
    stats = _stats(coverage_ha=survey_area_m2(area) / 10000.0, flight_s=st["flight_seconds"], distance_m=dist,
                   photos=st["photo_count"], photos_estimated=not full, waypoints=len(waypoints), lines=str(n_lines),
                   batteries=st["battery_count"])
    return {
        "flights": [{"waypoints": [[round(a, 9), round(b, 9)] for a, b in w], "heights": h, "headings": None,
                     "pitches": None, "actions": None, "label": None} for w, h in missions],
        "stats": stats, "info": terrain_error, "errors": [], "split": {"value": value, "max": mx},
        "direction": direction if s["auto_direction"] else None, "terrain_failed": terrain_error is not None,
        "overlays": {}, "legacy_actions": True,
    }


def _run_length(verts, a, b):
    return sum(math.hypot(verts[i + 1][0] - verts[i][0], verts[i + 1][1] - verts[i][1]) for i in range(a, b))


def _mid_vertex(verts, a, b):
    seg = [math.hypot(verts[i + 1][0] - verts[i][0], verts[i + 1][1] - verts[i][1]) for i in range(a, b)]
    total = sum(seg)
    if total <= 0:
        return a + 1
    half, acc, best, best_d = total / 2.0, 0.0, None, None
    for i in range(a, b):
        acc += seg[i - a]
        cand = i + 1
        if a < cand < b and (best_d is None or abs(acc - half) < best_d):
            best_d, best = abs(acc - half), cand
    return best if best is not None else a + 1


def corridor_groups(parts_m, breaks, target):
    groups = []
    for pi, verts in enumerate(parts_m):
        if len(verts) < 2:
            continue
        cuts = sorted(set(v for (p, v) in breaks if p == pi and 0 < v < len(verts) - 1))
        bounds = [0] + cuts + [len(verts) - 1]
        for a, b in zip(bounds, bounds[1:]):
            groups.append((pi, a, b))
    while len(groups) < target:
        splittable = [gr for gr in groups if gr[2] - gr[1] >= 2]
        if not splittable:
            break
        pi, a, b = max(splittable, key=lambda gr: _run_length(parts_m[gr[0]], gr[1], gr[2]))
        mid = _mid_vertex(parts_m[pi], a, b)
        if not a < mid < b:
            break
        groups.remove((pi, a, b))
        groups += [(pi, a, mid), (pi, mid, b)]
    groups.sort()
    return groups


def _split_stretch(waypoints, elevations, maxwp, altitude, base=None):
    if elevations and len(elevations) == len(waypoints):
        triples = [(lon, lat, e) for (lon, lat), e in zip(waypoints, elevations)]
        out = []
        for part in split_by_waypoint_count(triples, 1, maxwp):
            pe = [e for _, _, e in part]
            out.append(([(a, b) for a, b, _ in part], heights_above_takeoff(pe, altitude, base=base)))
        return out
    return [(p, None) for p in split_by_waypoint_count(waypoints, 1, maxwp)]


def _plan_corridor(s, g, drone):
    parts = line_parts(g.get("line"))
    if not parts:
        raise PlanError("Draw the corridor centre line on the map first.")
    clon, clat = _line_centroid(parts)
    fwd, _inv = transformers(utm_epsg(clon, clat))
    parts_m = []
    for part in parts:
        xs, ys = fwd.transform([p[0] for p in part], [p[1] for p in part])
        parts_m.append(list(zip(xs, ys)))
    length_m = 0.0
    for part in parts:
        for a, b in zip(part, part[1:]):
            length_m += GEOD.inv(a[0], a[1], b[0], b[1])[2]
    full = s["capture"] == "full"
    usable_min = drone["battery"] * (1.0 - BATTERY_RESERVE)
    across, _ = footprint(drone, s["altitude"])
    line_spacing = max(across * (1.0 - s["side"] / 100.0), 0.5)
    passes = len(compute_pass_offsets(s["buffer"], line_spacing, across))
    n_lines = passes * len(parts)
    est_dist = passes * length_m
    speed = s["speed"]
    if full:
        est_photos = est_dist / (full_auto_spacing(s, drone) or 1e9)
        per_photo = max(HALT_S, drone["interval"])
        est_min = (est_dist / speed + est_photos * per_photo) / 60.0 if speed > 0 else 0.0
    else:
        est_min = est_dist / (speed * 60.0) if speed > 0 else 0.0
    est_batteries = math.ceil(est_min / usable_min) if est_min > 0 else 1
    max_split = max(1, sum(max(1, len(v) - 1) for v in parts))
    value, mx = _split_value(s, max_split, est_batteries)
    target = value
    breaks = []
    for b in g.get("breaks") or []:
        try:
            breaks.append((int(b[0]), int(b[1])))
        except (TypeError, ValueError, IndexError):
            continue
    groups = corridor_groups(parts_m, breaks, target)
    densify = full_auto_spacing(s, drone) if full else None
    shot = full_auto_spacing(s, drone) if full else max(speed * drone["interval"], 0.5)
    missions = []
    shared_base = None
    terrain_error = None
    for pi, a, b in groups:
        try:
            wps, _ = generate_corridor_route([parts[pi][a:b + 1]], s["altitude"], shot, s["side"] / 100.0, s["buffer"], drone, densify)
        except ValueError:
            continue
        wps, elevs, err = _apply_terrain(s, wps, all_legs=True)
        terrain_error = terrain_error or err
        if s["same_takeoff"] and shared_base is None and elevs and len(elevs) == len(wps):
            shared_base = elevs[0]
        missions.extend(_split_stretch(wps, elevs, s["max_wp"], s["altitude"], base=shared_base if s["same_takeoff"] else None))
    if not missions or all(len(w) < 2 for w, _ in missions):
        raise PlanError("The corridor produced no waypoints. Check that the line has length and the buffer is greater than 0.")
    waypoints = [w for m, _ in missions for w in m]
    dist = sum(route_distance_m([{"longitude_deg": a, "latitude_deg": b} for a, b in m]) for m, _ in missions if len(m) >= 2)
    if full:
        n_photos = len(waypoints)
        per_photo = max(HALT_S, drone["interval"])
        flight_min = (dist / speed + n_photos * per_photo) / 60.0 if speed > 0 else 0.0
    else:
        n_photos = max(0, int(dist / shot))
        flight_min = dist / (speed * 60.0) if speed > 0 else 0.0
    batteries = math.ceil(flight_min / usable_min) if flight_min > 0 else 0
    stats = _stats(coverage_ha=length_m * 2.0 * s["buffer"] / 10000.0, flight_s=flight_min * 60, distance_m=dist,
                   photos=n_photos, photos_estimated=not full, waypoints=len(waypoints), lines=str(n_lines),
                   batteries=batteries, corridor_length_m=length_m)
    return {
        "flights": [{"waypoints": [[round(a, 9), round(b, 9)] for a, b in w], "heights": h, "headings": None,
                     "pitches": None, "actions": None, "label": None} for w, h in missions],
        "stats": stats, "info": terrain_error, "errors": [], "split": {"value": value, "max": mx},
        "direction": None, "terrain_failed": terrain_error is not None,
        "overlays": {"band": corridor_band(parts, s["buffer"])}, "legacy_actions": True,
    }


def plan(settings, geometry):
    s = clean_settings(settings)
    g = geometry if isinstance(geometry, dict) else {}
    drone = DRONES[s["drone"]]
    try:
        if s["kind"] == "corridor":
            p = _plan_corridor(s, g, drone)
        elif s["kind"] in ("orbit", "oblique", "panorama") or not s["terrain"]:
            p = _plan_shared(s, g, drone)
        else:
            p = _plan_2d_terrain(s, g, drone)
    except PlanningError as e:
        raise PlanError(e.message)
    except (KeyError, TypeError, IndexError):
        raise PlanError("The mission geometry is not valid.")
    p["stats"]["gsd"] = round(gsd_cm(drone, s["altitude"]), 2)
    if s["kind"] in ("2d", "oblique", "corridor"):
        p["stats"]["front_overlap_pct"] = s["front"] if s["capture"] == "full" else semi_front_overlap_pct(s, drone)
    p["settings"] = s
    return p


def panorama_points_problem(settings, geometry, points):
    s = clean_settings(settings)
    try:
        plan_panorama(_panorama_request(s, geometry or {}, DRONES[s["drone"]], points=[_pt(p) for p in points]))
    except PlanningError as e:
        if e.code == "points_too_close":
            return e.message + " Place it a little further away."
    except (PlanError, ValueError):
        pass
    return None


def compute_zones(parts, tolerance_m, altitude_m, same_takeoff, radius_m=TAKEOFF_RADIUS_M, steps=40):
    parts = [p for p in (parts or []) if p]
    if not parts:
        return None
    if same_takeoff:
        parts = parts[:1]
    spacing = max(10.0, (2.0 * radius_m) / steps)
    olon, olat = parts[0][0]
    fwd, inv = transformers(utm_epsg(olon, olat))
    zones = []
    sampled = 0
    for index, mission in enumerate(parts):
        lon, lat = mission[0]
        reference = TERRAIN.sample(lon, lat)
        cx, cy = fwd.transform(lon, lat)
        candidates = []
        disk_count = 0
        n = int(math.ceil(2 * radius_m / spacing))
        for i in range(n + 1):
            x = cx - radius_m + i * spacing
            for j in range(n + 1):
                y = cy - radius_m + j * spacing
                if math.hypot(x - cx, y - cy) > radius_m:
                    continue
                plon, plat = inv.transform(x, y)
                try:
                    e = TERRAIN.sample(plon, plat)
                except TerrainError:
                    continue
                candidates.append((x, y, e))
                disk_count += 1
        sampled += len(candidates)
        cells = [(x, y) for x, y, e in candidates if abs(e - reference) <= tolerance_m and math.hypot(x - cx, y - cy) <= radius_m]
        centre = ogr.Geometry(ogr.wkbPoint)
        centre.AddPoint_2D(cx, cy)
        disk = centre.Buffer(radius_m, 12)
        if disk_count > 0 and len(cells) == disk_count:
            region = disk
        else:
            if not cells:
                continue
            coll = ogr.Geometry(ogr.wkbGeometryCollection)
            for x, y in cells:
                p = ogr.Geometry(ogr.wkbPoint)
                p.AddPoint_2D(x, y)
                coll.AddGeometry(p.Buffer(spacing, 2))
            region = coll.UnaryUnion().Intersection(disk)
            if region.IsEmpty():
                continue
        gj = _to_geojson(region, inv)
        if gj:
            zones.append({"index": index, "ref_elev": round(reference, 1), "geometry": gj})
    return {"zones": zones, "gsd_var": 100.0 * tolerance_m / altitude_m if altitude_m > 0 else 0.0,
            "sampled": sampled, "radius_m": radius_m}


def _flight_spec(s, flight, name, create_ms=None):
    kind = s["kind"]
    drone = DRONES[s["drone"]]
    orbit = kind == "orbit"
    point = orbit or kind == "panorama"
    wps = [(float(a), float(b)) for a, b in flight["waypoints"]]
    heights = flight.get("heights")
    pitches = flight.get("pitches")
    headings = flight.get("headings")
    flight_specific = kind in ("orbit", "oblique", "panorama")
    if not flight_specific:
        pitches = headings = None
    offset = 0.0 if point else s["launch_offset"]
    altitude = heights[0] if (flight_specific and heights) else s["altitude"]
    pitch = pitches[0] if pitches else (s["rings"][0][2] if orbit else -90)
    if pitches is not None:
        pitches = [int(p) if float(p).is_integer() else p for p in pitches]
    if isinstance(pitch, float) and pitch.is_integer():
        pitch = int(pitch)
    actions = flight.get("actions")
    return drone["enum"], wpml.MissionSpec(
        waypoints=wps, altitude_m=altitude - offset, speed_ms=s["speed"], finish_action=s["finish"],
        rc_lost_action=s["rc_lost"], gimbal_pitch=pitch, mission_name=name, create_time_ms=create_ms,
        capture_mode=s["capture"], actions=actions,
        heights=[h - offset for h in heights] if heights is not None else None,
        curved_path=s["path"] == "curved", headings=headings, gimbal_pitches=pitches,
    )


def kmz(settings, flight, name, create_ms=None):
    s = clean_settings(settings)
    enum, spec = _flight_spec(s, flight, name, create_ms)
    return wpml.write(enum, spec)


def export_files(settings, flights, name, part=None):
    s = clean_settings(settings)
    count = len(flights)
    base = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip() or "Mission"
    files = []
    width = len(str(count))
    for index, flight in enumerate(flights, start=1):
        if part is not None and index - 1 != part:
            continue
        if count == 1:
            fname, mname = base + ".kmz", name
        else:
            fname = "%s_%s_of_%d.kmz" % (base, str(index).zfill(width), count)
            mname = "%s %d of %d" % (name, index, count)
        enum, spec = _flight_spec(s, flight, mname)
        files.append((fname, wpml.write(enum, spec)))
    return files, base


def zip_files(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for fname, data in files:
            z.writestr(fname, data)
    return buf.getvalue()


def import_geometry(name, data):
    from osgeo import gdal
    gdal.UseExceptions()
    ext = os.path.splitext(name.lower())[1]
    path = "/vsimem/mission_planner_import_%d%s" % (threading.get_ident(), ext or ".json")
    gdal.FileFromMemBuffer(path, data)
    polys, lines, points = [], [], []
    try:
        src = path
        if ext == ".kmz":
            listing = gdal.ReadDir("/vsizip/" + path) or []
            kml = next((f for f in listing if f.lower().endswith(".kml")), None)
            if not kml:
                raise PlanError("No KML file found in %s." % name)
            src = "/vsizip/" + path + "/" + kml
        ds = ogr.Open(src)
        if ds is None:
            raise PlanError("Could not read %s." % name)
        from osgeo import osr
        wgs = osr.SpatialReference()
        wgs.ImportFromEPSG(4326)
        wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        for li in range(ds.GetLayerCount()):
            layer = ds.GetLayer(li)
            srs = layer.GetSpatialRef()
            ct = None
            if srs is not None:
                srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                if not srs.IsSame(wgs):
                    ct = osr.CoordinateTransformation(srs, wgs)
            for feat in layer:
                g = feat.GetGeometryRef()
                if g is None:
                    continue
                g = g.Clone()
                if ct is not None:
                    g.Transform(ct)
                _collect(g, polys, lines, points)
    except PlanError:
        raise
    except Exception:
        raise PlanError("Could not read %s." % name)
    finally:
        gdal.Unlink(path)
    return {"polygons": polys, "lines": lines, "points": points}


def _collect(g, polys, lines, points):
    t = ogr.GT_Flatten(g.GetGeometryType())
    if t == ogr.wkbPolygon:
        rings = []
        for i in range(g.GetGeometryCount()):
            pts = [[p[0], p[1]] for p in (g.GetGeometryRef(i).GetPoints() or [])]
            if len(pts) >= 4:
                rings.append(pts)
        if rings:
            polys.append(rings)
    elif t == ogr.wkbLineString:
        pts = [[p[0], p[1]] for p in (g.GetPoints() or [])]
        if len(pts) >= 2:
            lines.append(pts)
    elif t == ogr.wkbPoint:
        points.append([g.GetX(), g.GetY()])
    elif t in (ogr.wkbMultiPolygon, ogr.wkbMultiLineString, ogr.wkbMultiPoint, ogr.wkbGeometryCollection):
        for i in range(g.GetGeometryCount()):
            _collect(g.GetGeometryRef(i), polys, lines, points)


def drones_json():
    return json.dumps([{k: d[k] for k in ("code", "name", "info", "sw", "sh", "f", "iw", "ih", "interval", "min_speed", "max_speed", "battery", "battery_safe")} for d in DRONES.values()])


def config_json():
    return json.dumps({"defaults": DEFAULTS, "auto_spacing": AUTO_SPACING_PER_ALTITUDE, "takeoff_radius": TAKEOFF_RADIUS_M,
                       "finish": list(FINISH_ACTIONS), "rc_lost": list(RC_LOST_ACTIONS), "max_rings": MAX_ORBIT_RINGS})


def _numbers(v, n, lo, hi):
    if v is None:
        return None
    if not isinstance(v, list) or len(v) != n:
        raise PlanError("The flight plan is not valid.")
    out = []
    for x in v:
        if x is None:
            out.append(None)
            continue
        x = float(x)
        if not math.isfinite(x) or not lo <= x <= hi:
            raise PlanError("The flight plan is not valid.")
        out.append(x)
    return out


def clean_flights(flights):
    if not isinstance(flights, list) or not flights or len(flights) > 999:
        raise PlanError("Plan the mission first.")
    out = []
    try:
        for f in flights:
            wps = [_pt(p) for p in f["waypoints"]]
            if not wps or len(wps) > 5000:
                raise PlanError("The flight plan is not valid.")
            n = len(wps)
            actions = f.get("actions")
            if actions is not None:
                clean = []
                for a in actions:
                    t = a.get("type")
                    if t not in ("rotate_camera", "hover", "take_photo"):
                        raise PlanError("The flight plan is not valid.")
                    item = {"type": t, "waypoint_index": int(a["waypoint_index"])}
                    if t == "rotate_camera":
                        item["pitch_deg"] = float(a["pitch_deg"])
                    if t == "hover":
                        item["duration_s"] = float(a["duration_s"])
                    clean.append(item)
                actions = clean
            out.append({
                "waypoints": wps,
                "heights": _numbers(f.get("heights"), n, -2000, 2000),
                "headings": _numbers(f.get("headings"), n, -180, 180),
                "pitches": _numbers(f.get("pitches"), n, -90, 90),
                "actions": actions,
            })
    except (KeyError, TypeError, ValueError, AttributeError):
        raise PlanError("The flight plan is not valid.")
    return out
