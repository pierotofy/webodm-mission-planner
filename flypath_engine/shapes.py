import math

from osgeo import ogr

ogr.UseExceptions()

_TYPES = {
    ogr.wkbPoint: "Point",
    ogr.wkbLineString: "LineString",
    ogr.wkbLinearRing: "LinearRing",
    ogr.wkbPolygon: "Polygon",
    ogr.wkbMultiPoint: "MultiPoint",
    ogr.wkbMultiLineString: "MultiLineString",
    ogr.wkbMultiPolygon: "MultiPolygon",
    ogr.wkbGeometryCollection: "GeometryCollection",
}


class Geom:
    __slots__ = ("g", "prec")

    def __init__(self, g, prec=0.0):
        self.g = g
        self.prec = prec

    def _out(self, g, prec=None):
        prec = self.prec if prec is None else prec
        if not prec:
            return Geom(g)
        s = 1.0 / prec
        _map_leaf(g, lambda x, y: (math.floor(x * s + 0.5) / s, math.floor(y * s + 0.5) / s))
        return Geom(g, prec)

    @property
    def geom_type(self):
        return _TYPES.get(ogr.GT_Flatten(self.g.GetGeometryType()), "Unknown")

    @property
    def is_empty(self):
        return self.g.IsEmpty()

    @property
    def is_valid(self):
        return self.g.IsValid()

    @property
    def area(self):
        return self.g.GetArea()

    @property
    def length(self):
        return self.g.Length()

    @property
    def bounds(self):
        a, b, c, d = self.g.GetEnvelope()
        return a, c, b, d

    @property
    def centroid(self):
        return self._out(self.g.Centroid())

    @property
    def x(self):
        return self.g.GetX()

    @property
    def y(self):
        return self.g.GetY()

    @property
    def coords(self):
        return [p[:2] for p in self.g.GetPoints() or []]

    @property
    def geoms(self):
        if self.geom_type in ("Polygon", "LineString", "LinearRing", "Point"):
            return ()
        return [Geom(self.g.GetGeometryRef(i).Clone()) for i in range(self.g.GetGeometryCount())]

    @property
    def exterior(self):
        return Geom(self.g.GetGeometryRef(0).Clone())

    @property
    def interiors(self):
        return [Geom(self.g.GetGeometryRef(i).Clone()) for i in range(1, self.g.GetGeometryCount())]

    def buffer(self, distance, quad_segs=16, join_style=None, mitre_limit=5.0):
        if join_style == "mitre":
            return self._out(self.g.Buffer(distance, ["JOIN_STYLE=MITRE", "MITRE_LIMIT=%g" % mitre_limit, "QUADRANT_SEGMENTS=%d" % quad_segs]))
        return self._out(self.g.Buffer(distance, quad_segs))

    def intersection(self, other):
        return self._out(self.g.Intersection(other.g), max(self.prec, other.prec))

    def union(self, other):
        return self._out(self.g.Union(other.g), max(self.prec, other.prec))

    def covers(self, other):
        return self.g.Intersects(other.g) if other.geom_type == "Point" else self.g.Contains(other.g)

    def representative_point(self):
        return self._out(self.g.PointOnSurface())


def _ring(coords, kind=ogr.wkbLinearRing):
    r = ogr.Geometry(kind)
    for c in coords:
        r.AddPoint_2D(float(c[0]), float(c[1]))
    if kind == ogr.wkbLinearRing:
        r.CloseRings()
    return r


def Polygon(exterior, holes=None):
    g = ogr.Geometry(ogr.wkbPolygon)
    g.AddGeometry(_ring(exterior))
    for h in holes or ():
        g.AddGeometry(_ring(h))
    return Geom(g)


def MultiPolygon(polygons):
    g = ogr.Geometry(ogr.wkbMultiPolygon)
    for p in polygons:
        g.AddGeometry(p.g)
    return Geom(g)


def LineString(coords):
    return Geom(_ring(coords, ogr.wkbLineString))


def Point(xy):
    g = ogr.Geometry(ogr.wkbPoint)
    g.AddPoint_2D(float(xy[0]), float(xy[1]))
    return Geom(g)


def _map_leaf(g, fn):
    n = g.GetGeometryCount()
    if n:
        for i in range(n):
            _map_leaf(g.GetGeometryRef(i), fn)
        return
    for i in range(g.GetPointCount()):
        x, y = fn(g.GetX(i), g.GetY(i))
        g.SetPoint_2D(i, x, y)


def transform(fn, geom):
    g = geom.g.Clone()
    _map_leaf(g, fn)
    return Geom(g)


def rotate(geom, angle_deg, origin):
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    ox, oy = origin

    def fn(x, y):
        dx, dy = x - ox, y - oy
        return ox + dx * c - dy * s, oy + dx * s + dy * c

    return transform(fn, geom)


def translate(geom, xoff=0.0, yoff=0.0):
    return transform(lambda x, y: (x + xoff, y + yoff), geom)


def unary_union(geoms):
    c = ogr.Geometry(ogr.wkbGeometryCollection)
    prec = 0.0
    for g in geoms:
        c.AddGeometry(g.g)
        prec = max(prec, g.prec)
    return Geom(c)._out(c.UnaryUnion(), prec)


def set_precision(geom, grid):
    g = Geom(geom.g.Clone())._out(geom.g.Clone(), grid)
    if g.geom_type in ("Polygon", "MultiPolygon") and not g.is_valid:
        g = g.buffer(0)
    return g


def _edges(geom):
    polys = geom.geoms if geom.geom_type == "MultiPolygon" else (geom,)
    res = []
    for p in polys:
        edges = []
        for i in range(p.g.GetGeometryCount()):
            pts = p.g.GetGeometryRef(i).GetPoints() or []
            for a, b in zip(pts, pts[1:]):
                edges.append((a[0], a[1], b[0], b[1]))
        res.append(edges)
    return res


class Scanner:
    __slots__ = ("polys",)

    def __init__(self, geom):
        self.polys = _edges(geom)

    def intervals(self, x):
        out = []
        for edges in self.polys:
            ys = []
            for x1, y1, x2, y2 in edges:
                if (x1 <= x < x2) or (x2 <= x < x1):
                    ys.append(y1 + (x - x1) * (y2 - y1) / (x2 - x1))
            ys.sort()
            for i in range(0, len(ys) - 1, 2):
                if ys[i + 1] > ys[i]:
                    out.append((ys[i], ys[i + 1]))
        return out
