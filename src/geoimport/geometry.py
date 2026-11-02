"""Koordinatenreferenzsysteme und GML-Geometrien.

Das Modul liest GML-Geometrien (GML 2, 3.1.1 und 3.2) in Shapely-Objekte ein.
Die Achsenreihenfolge wird aus dem srsName abgeleitet: Bei URN- und
URI-Schreibweisen (z. B. ``urn:ogc:def:crs:EPSG::4326``) gilt die Reihenfolge
laut EPSG-Register (bei EPSG:4326 also Breite/Länge), bei der Kurzform
``EPSG:4326`` die in der Praxis übliche Reihenfolge Länge/Breite (x/y).
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

import shapely
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.errors import GEOSException
from shapely.geometry import (
    GeometryCollection,
    LinearRing,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    box,
)
from shapely.geometry.base import BaseGeometry

GML_NS_PREFIX = "http://www.opengis.net/gml"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

_EPSG_PATTERNS = [
    # urn:ogc:def:crs:EPSG::25833, urn:ogc:def:crs:EPSG:6.6:4326, urn:x-ogc:def:crs:EPSG:4326
    (re.compile(r"^urn:(?:x-)?ogc:def:crs:epsg:[\d.]*:(\d+)$", re.I), True),
    # http://www.opengis.net/def/crs/EPSG/0/4326
    (re.compile(r"^https?://www\.opengis\.net/def/crs/epsg/[^/]+/(\d+)$", re.I), True),
    # http://www.opengis.net/gml/srs/epsg.xml#4326 (traditionell x/y)
    (re.compile(r"^https?://www\.opengis\.net/gml/srs/epsg\.xml#(\d+)$", re.I), False),
    # EPSG:4326 (traditionell x/y)
    (re.compile(r"^epsg:(\d+)$", re.I), False),
]
_CRS84 = re.compile(
    r"^(crs:84|ogc:crs84|urn:ogc:def:crs:ogc:(?:[\d.]*:)?crs84|"
    r"https?://www\.opengis\.net/def/crs/ogc/[^/]+/crs84)$",
    re.I,
)


class GeometryError(ValueError):
    """Geometrie konnte nicht gelesen werden."""


class UnknownCrsError(ValueError):
    """Koordinatenreferenzsystem ist unbekannt oder nicht unterstützt."""


def _parse_srs(srs_name: str) -> tuple[int, bool]:
    """Liefert (EPSG-Code, Achsenreihenfolge laut Register maßgeblich)."""
    value = srs_name.strip()
    if _CRS84.match(value):
        return 4326, False
    if value.isdigit():
        return int(value), False
    for pattern, authority_axis in _EPSG_PATTERNS:
        match = pattern.match(value)
        if match:
            return int(match.group(1)), authority_axis
    raise UnknownCrsError(f"Koordinatenreferenzsystem nicht erkannt: {srs_name!r}")


@lru_cache(maxsize=256)
def _crs_info(epsg: int) -> tuple[bool, bool]:
    """(Nord-/Süd-Achse zuerst, geographisches KBS) für einen EPSG-Code."""
    try:
        crs = CRS.from_epsg(epsg)
    except CRSError as exc:
        raise UnknownCrsError(f"EPSG:{epsg} ist unbekannt") from exc
    axes = crs.axis_info
    north_first = bool(axes) and axes[0].direction.lower() in ("north", "south")
    return north_first, bool(crs.is_geographic)


def srid_from_crs(srs_name: str) -> int:
    """Wandelt eine CRS-Angabe (EPSG:xxxx, URN, URI, CRS84) in einen EPSG-Code um."""
    epsg, _ = _parse_srs(srs_name)
    _crs_info(epsg)  # prüft, ob der Code bekannt ist
    return epsg


def is_geographic(srid: int) -> bool:
    return _crs_info(srid)[1]


def needs_axis_swap(srs_name: str | None) -> bool:
    """True, wenn Koordinaten in Reihenfolge y/x (z. B. Breite/Länge) vorliegen."""
    if not srs_name:
        return False
    epsg, authority_axis = _parse_srs(srs_name)
    if not authority_axis:
        return False
    return _crs_info(epsg)[0]


def to_wkb(geometry: BaseGeometry) -> bytes:
    """Zweidimensionales WKB (Little Endian) für Hashbildung und Datenbank."""
    return shapely.to_wkb(geometry, output_dimension=2, byte_order=1, include_srid=False)


# --------------------------------------------------------------------------- GML


def local_name(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1] if tag.startswith("{") else tag.split(":")[-1]


def namespace(tag: Any) -> str:
    if isinstance(tag, str) and tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return ""


def is_gml(elem: Any) -> bool:
    return namespace(elem.tag).startswith(GML_NS_PREFIX)


GML_GEOMETRY_TYPES = {
    "Point",
    "LineString",
    "LinearRing",
    "Curve",
    "Polygon",
    "Surface",
    "PolyhedralSurface",
    "TriangulatedSurface",
    "Tin",
    "MultiPoint",
    "MultiCurve",
    "MultiLineString",
    "MultiSurface",
    "MultiPolygon",
    "MultiGeometry",
    "CompositeCurve",
    "CompositeSurface",
    "Envelope",
}


def is_gml_geometry(elem: Any) -> bool:
    return is_gml(elem) and local_name(elem.tag) in GML_GEOMETRY_TYPES


def find_geometry_element(prop: Any) -> Any | None:
    """Erste GML-Geometrie unterhalb eines Eigenschaftselements."""
    for child in prop:
        if is_gml_geometry(child):
            return child
    return None


def _children(elem: Any, *names: str) -> list[Any]:
    return [c for c in elem if is_gml(c) and local_name(c.tag) in names]


def _numbers(text: str | None) -> list[float]:
    if not text:
        return []
    try:
        return [float(v) for v in text.split()]
    except ValueError as exc:
        raise GeometryError(f"Ungültige Koordinatenangabe: {text[:60]!r}") from exc


class _Ctx:
    __slots__ = ("srs", "dim", "swap")

    def __init__(self, srs: str | None, dim: int, swap: bool):
        self.srs = srs
        self.dim = dim
        self.swap = swap

    def derive(self, elem: Any) -> _Ctx:
        srs = elem.get("srsName") or self.srs
        dim_attr = elem.get("srsDimension")
        dim = int(dim_attr) if dim_attr else self.dim
        swap = needs_axis_swap(srs) if srs != self.srs else self.swap
        return _Ctx(srs, dim, swap)


def _pairs(values: list[float], ctx: _Ctx) -> list[tuple[float, float]]:
    dim = ctx.dim if ctx.dim >= 2 else 2
    if len(values) % dim:
        raise GeometryError(
            f"Anzahl der Koordinatenwerte ({len(values)}) passt nicht zur Dimension {dim}"
        )
    out = []
    for i in range(0, len(values), dim):
        a, b = values[i], values[i + 1]
        out.append((b, a) if ctx.swap else (a, b))
    return out


def _gml2_coordinates(elem: Any, ctx: _Ctx) -> list[tuple[float, float]]:
    cs = elem.get("cs", ",")
    ts = elem.get("ts", " ")
    dec = elem.get("decimal", ".")
    out = []
    for tup in (elem.text or "").strip().split(ts if ts.strip() else None):
        if not tup:
            continue
        parts = tup.split(cs)
        if dec != ".":
            parts = [p.replace(dec, ".") for p in parts]
        try:
            a, b = float(parts[0]), float(parts[1])
        except (ValueError, IndexError) as exc:
            raise GeometryError(f"Ungültige gml:coordinates: {tup!r}") from exc
        out.append((b, a) if ctx.swap else (a, b))
    return out


def _coords(elem: Any, ctx: _Ctx) -> list[tuple[float, float]]:
    """Koordinaten aus posList, pos, coordinates, coord oder pointProperty."""
    coords: list[tuple[float, float]] = []
    for child in elem:
        if not is_gml(child):
            continue
        name = local_name(child.tag)
        cctx = ctx.derive(child)
        if name == "posList":
            coords.extend(_pairs(_numbers(child.text), cctx))
        elif name == "pos":
            values = _numbers(child.text)
            if len(values) < 2:
                raise GeometryError("gml:pos mit weniger als zwei Werten")
            a, b = values[0], values[1]
            coords.append((b, a) if cctx.swap else (a, b))
        elif name == "coordinates":
            coords.extend(_gml2_coordinates(child, cctx))
        elif name == "coord":
            x = child.find("{*}X")
            y = child.find("{*}Y")
            if x is None or y is None:
                raise GeometryError("gml:coord ohne X/Y")
            a, b = float(x.text), float(y.text)
            coords.append((b, a) if cctx.swap else (a, b))
        elif name in ("pointProperty", "pointRep"):
            point = find_geometry_element(child)
            if point is not None:
                coords.extend(_coords(point, cctx.derive(point)))
    return coords


def _ring(container: Any, ctx: _Ctx) -> list[tuple[float, float]]:
    """Ring aus gml:exterior/interior (LinearRing oder Ring mit curveMember)."""
    for child in container:
        if not is_gml(child):
            continue
        name = local_name(child.tag)
        cctx = ctx.derive(child)
        if name == "LinearRing":
            return _coords(child, cctx)
        if name == "Ring":
            coords: list[tuple[float, float]] = []
            for member in _children(child, "curveMember"):
                curve = find_geometry_element(member)
                if curve is None:
                    continue
                part = list(_line(curve, cctx.derive(curve)).coords)
                if coords and part and coords[-1] == part[0]:
                    part = part[1:]
                coords.extend(part)
            return coords
    raise GeometryError("Ring ohne LinearRing")


def _line(elem: Any, ctx: _Ctx) -> LineString:
    name = local_name(elem.tag)
    if name in ("LineString", "LinearRing", "LineStringSegment"):
        return LineString(_coords(elem, ctx))
    if name in ("Curve", "CompositeCurve"):
        coords: list[tuple[float, float]] = []
        segments = _children(elem, "segments")
        parts: list[Any] = []
        if segments:
            parts = [s for s in segments[0] if is_gml(s)]
        else:  # CompositeCurve
            for member in _children(elem, "curveMember"):
                g = find_geometry_element(member)
                if g is not None:
                    parts.append(g)
        for seg in parts:
            seg_name = local_name(seg.tag)
            if seg_name not in ("LineStringSegment", "LineString", "Curve"):
                raise GeometryError(f"Kurvensegment {seg_name} wird nicht unterstützt")
            pts = list(_line(seg, ctx.derive(seg)).coords)
            if coords and pts and coords[-1] == pts[0]:
                pts = pts[1:]
            coords.extend(pts)
        return LineString(coords)
    raise GeometryError(f"{name} ist keine Liniengeometrie")


def _polygon(elem: Any, ctx: _Ctx) -> Polygon:
    exterior = _children(elem, "exterior", "outerBoundaryIs")
    if not exterior:
        raise GeometryError("Polygon ohne äußeren Ring")
    shell = _ring(exterior[0], ctx)
    holes = [_ring(i, ctx) for i in _children(elem, "interior", "innerBoundaryIs")]
    for ring in [shell, *holes]:
        if len(ring) < 4:
            raise GeometryError("Ring mit weniger als vier Punkten")
    return Polygon(LinearRing(shell), [LinearRing(h) for h in holes])


def _polygons(elem: Any, ctx: _Ctx) -> list[Polygon]:
    name = local_name(elem.tag)
    if name in ("Polygon", "PolygonPatch", "Triangle", "Rectangle"):
        return [_polygon(elem, ctx)]
    if name in ("Surface", "PolyhedralSurface", "TriangulatedSurface", "Tin"):
        patches = _children(elem, "patches", "polygonPatches", "trianglePatches")
        out: list[Polygon] = []
        for container in patches:
            for patch in container:
                if is_gml(patch):
                    out.extend(_polygons(patch, ctx.derive(patch)))
        return out
    if name in ("MultiSurface", "MultiPolygon", "CompositeSurface"):
        out = []
        for member in _children(
            elem, "surfaceMember", "surfaceMembers", "polygonMember", "polygonMembers"
        ):
            for g in member:
                if is_gml_geometry(g):
                    out.extend(_polygons(g, ctx.derive(g)))
        return out
    raise GeometryError(f"{name} ist keine Flächengeometrie")


def _members(elem: Any, *names: str) -> list[Any]:
    out = []
    for member in _children(elem, *names):
        if member.get(XLINK_HREF) is not None and len(member) == 0:
            raise GeometryError("Verweise (xlink:href) auf Geometrien werden nicht unterstützt")
        out.extend(g for g in member if is_gml_geometry(g))
    return out


def _parse(elem: Any, ctx: _Ctx) -> BaseGeometry:
    name = local_name(elem.tag)
    if name == "Point":
        pts = _coords(elem, ctx)
        if not pts:
            raise GeometryError("Punkt ohne Koordinaten")
        return Point(pts[0])
    if name in ("LineString", "Curve", "CompositeCurve"):
        return _line(elem, ctx)
    if name == "LinearRing":
        return LineString(_coords(elem, ctx))
    if name in ("Polygon", "Surface", "PolyhedralSurface", "TriangulatedSurface", "Tin"):
        polys = _polygons(elem, ctx)
        if len(polys) == 1:
            return polys[0]
        return MultiPolygon(polys)
    if name in ("MultiSurface", "MultiPolygon", "CompositeSurface"):
        return MultiPolygon(_polygons(elem, ctx))
    if name == "MultiPoint":
        pts = []
        for g in _members(elem, "pointMember", "pointMembers"):
            p = _parse(g, ctx.derive(g))
            pts.append(p)
        return MultiPoint(pts)
    if name in ("MultiCurve", "MultiLineString"):
        lines = []
        for g in _members(elem, "curveMember", "curveMembers", "lineStringMember"):
            geom = _parse(g, ctx.derive(g))
            if isinstance(geom, MultiLineString):
                lines.extend(geom.geoms)
            else:
                lines.append(geom)
        return MultiLineString(lines)
    if name == "MultiGeometry":
        return GeometryCollection(
            [_parse(g, ctx.derive(g)) for g in _members(elem, "geometryMember", "geometryMembers")]
        )
    if name == "Envelope":
        lower = _children(elem, "lowerCorner")
        upper = _children(elem, "upperCorner")
        if not lower or not upper:
            raise GeometryError("Envelope ohne lowerCorner/upperCorner")
        corners = []
        for corner in (lower[0], upper[0]):
            values = _numbers(corner.text)
            if len(values) < 2:
                raise GeometryError("Envelope-Ecke mit weniger als zwei Werten")
            a, b = values[0], values[1]
            corners.append((b, a) if ctx.swap else (a, b))
        (x1, y1), (x2, y2) = corners
        return box(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
    raise GeometryError(f"GML-Geometrietyp {name} wird nicht unterstützt")


def parse_gml_geometry(
    elem: Any, default_srs: str | None = None
) -> tuple[BaseGeometry, str | None]:
    """Liest ein GML-Geometrieelement. Liefert (Geometrie, srsName)."""
    base = _Ctx(default_srs, 2, needs_axis_swap(default_srs) if default_srs else False)
    ctx = base.derive(elem)
    try:
        geom = _parse(elem, ctx)
    except (ValueError, TypeError, IndexError, GEOSException) as exc:
        if isinstance(exc, GeometryError):
            raise
        raise GeometryError(str(exc)) from exc
    return geom, ctx.srs
