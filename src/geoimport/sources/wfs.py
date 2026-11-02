"""Quelladapter für OGC Web Feature Services (WFS 2.0.0, Rückfall auf 1.1.0).

Ablauf je Quelle:

1. ``GetCapabilities``: Version, Ausgabeformate, ``CountDefault``,
   ``ImplementsResultPaging`` und Standard-KBS des Feature-Typs ermitteln.
2. ``DescribeFeatureType``: erklärte Felder für die Schemaprüfung.
3. ``GetFeature`` seitenweise über ``STARTINDEX``/``COUNT``; GeoJSON, wenn der
   Dienst es anbietet, sonst GML 3.2 (bei WFS 1.1.0 GML 3.1.1, ohne Paginierung).
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from defusedxml import ElementTree as SafeET
from shapely.errors import GEOSException
from shapely.geometry import shape

from geoimport.config import WfsSource
from geoimport.geometry import (
    XLINK_HREF,
    GeometryError,
    UnknownCrsError,
    find_geometry_element,
    is_gml,
    local_name,
    namespace,
    parse_gml_geometry,
    srid_from_crs,
)
from geoimport.http import HttpError
from geoimport.models import RawRecord
from geoimport.sources.base import SourceAdapter, SourceError, register

log = logging.getLogger(__name__)

XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
GML32_FORMATS = (
    "application/gml+xml; version=3.2",
    "text/xml; subtype=gml/3.2.1",
    'text/xml; subtype="gml/3.2.1"',
    "text/xml; subtype=gml/3.2",
)
MAX_PAGES = 100_000
_GEOM_TYPE_WORDS = ("Point", "Curve", "Surface", "Polygon", "LineString", "Geometry", "Solid")


class WfsError(SourceError):
    """Fehler, den ein WFS gemeldet hat oder der beim Lesen der Antwort auftrat."""


# ------------------------------------------------------------------ XML-Helfer


def _parse_xml(content: bytes) -> Any:
    try:
        return SafeET.fromstring(content)
    except (SafeET.ParseError, ValueError) as exc:  # ValueError: von defusedxml abgewiesen
        raise WfsError(f"Antwort ist kein gültiges XML: {exc}") from exc


def _exception_text(root: Any) -> str:
    parts = []
    for elem in root.iter():
        if local_name(elem.tag) == "Exception":
            code = elem.get("exceptionCode") or elem.get("code") or ""
            texts = [
                (t.text or "").strip()
                for t in elem.iter()
                if local_name(t.tag) == "ExceptionText" and t.text
            ]
            locator = elem.get("locator")
            msg = " ".join(texts) or (elem.text or "").strip()
            label = code + (f" ({locator})" if locator else "")
            parts.append(f"{label}: {msg}" if label else msg)
        elif local_name(elem.tag) == "ServiceException":
            parts.append(((elem.get("code") or "") + " " + (elem.text or "").strip()).strip())
    return "; ".join(p for p in parts if p) or "unbekannter Fehler"


def raise_for_exception(content: bytes | None) -> None:
    """Wirft :class:`WfsError`, wenn die Antwort ein OWS-ExceptionReport ist."""
    if not content:
        return
    head = content[:4096].lstrip()
    if not head.startswith(b"<"):
        return
    if b"ExceptionReport" not in head and b"ServiceExceptionReport" not in head:
        return
    try:
        root = SafeET.fromstring(content)
    except (SafeET.ParseError, ValueError):
        return
    if local_name(root.tag) in ("ExceptionReport", "ServiceExceptionReport"):
        raise WfsError(f"WFS meldet Fehler: {_exception_text(root)}")


def _first_text(elem: Any, *names: str) -> str | None:
    for child in elem:
        if local_name(child.tag) in names and child.text and child.text.strip():
            return child.text.strip()
    return None


# ------------------------------------------------------------------ Capabilities


@dataclass
class FeatureTypeInfo:
    name: str
    title: str | None = None
    default_crs: str | None = None
    other_crs: list[str] = field(default_factory=list)
    output_formats: list[str] = field(default_factory=list)


@dataclass
class Capabilities:
    version: str
    title: str | None = None
    output_formats: list[str] = field(default_factory=list)
    count_default: int | None = None
    implements_paging: bool | None = None
    get_feature_url: str | None = None
    feature_types: list[FeatureTypeInfo] = field(default_factory=list)

    def find_feature_type(self, name: str) -> FeatureTypeInfo | None:
        for ft in self.feature_types:
            if ft.name == name:
                return ft
        wanted = name.split(":")[-1]
        matches = [ft for ft in self.feature_types if ft.name.split(":")[-1] == wanted]
        return matches[0] if len(matches) == 1 else None


def _constraint(elem: Any, caps: Capabilities) -> None:
    name = (elem.get("name") or "").lower()
    value = _first_text(elem, "DefaultValue") or _first_text(elem, "Value")
    if value is None:
        return
    if name == "countdefault":
        caps.count_default = _to_int(value)
    elif name == "implementsresultpaging":
        caps.implements_paging = value.strip().lower() == "true"


def _is_output_format(param: Any) -> bool:
    return (param.get("name") or "").lower() == "outputformat"


def _values(param: Any) -> list[str]:
    return [
        v.text.strip()
        for v in param.iter()
        if local_name(v.tag) == "Value" and v.text and v.text.strip()
    ]


def parse_capabilities(content: bytes) -> Capabilities:
    raise_for_exception(content)
    root = _parse_xml(content)
    if local_name(root.tag) != "WFS_Capabilities":
        raise WfsError(f"Unerwartete Antwort auf GetCapabilities: <{local_name(root.tag)}>")
    caps = Capabilities(version=root.get("version") or "")
    for elem in root:
        lname = local_name(elem.tag)
        if lname == "ServiceIdentification":
            caps.title = _first_text(elem, "Title")
        elif lname == "OperationsMetadata":
            global_formats: list[str] = []
            for op in elem:
                op_name = local_name(op.tag)
                if op_name == "Constraint":
                    _constraint(op, caps)
                elif op_name == "Parameter" and _is_output_format(op):
                    global_formats = _values(op)  # gilt für alle Operationen
                elif op_name == "Operation" and op.get("name") == "GetFeature":
                    for item in op:
                        item_name = local_name(item.tag)
                        if item_name == "Parameter" and _is_output_format(item):
                            caps.output_formats = _values(item)
                        elif item_name == "Constraint":
                            _constraint(item, caps)
                    for sub in op.iter():
                        if local_name(sub.tag) == "Get" and sub.get(XLINK_HREF):
                            caps.get_feature_url = sub.get(XLINK_HREF)
                            break
            if not caps.output_formats:
                caps.output_formats = global_formats
        elif lname == "FeatureTypeList":
            for ft_elem in elem:
                if local_name(ft_elem.tag) != "FeatureType":
                    continue
                name = _first_text(ft_elem, "Name")
                if not name:
                    continue
                info = FeatureTypeInfo(
                    name=name,
                    title=_first_text(ft_elem, "Title"),
                    default_crs=_first_text(ft_elem, "DefaultCRS", "DefaultSRS"),
                    other_crs=[
                        c.text.strip()
                        for c in ft_elem
                        if local_name(c.tag) in ("OtherCRS", "OtherSRS") and c.text
                    ],
                )
                for sub in ft_elem:
                    if local_name(sub.tag) == "OutputFormats":
                        info.output_formats = [
                            f.text.strip() for f in sub if f.text and f.text.strip()
                        ]
                caps.feature_types.append(info)
    return caps


# ------------------------------------------------------------------ DescribeFeatureType


def _is_geometry_type(type_name: str) -> bool:
    lname = type_name.split(":")[-1]
    return lname.endswith("PropertyType") and any(w in lname for w in _GEOM_TYPE_WORDS)


def _property_elements(node: Any) -> Iterator[Any]:
    """Elementdeklarationen einer complexType-Definition (ohne verschachtelte Typen)."""
    for child in node:
        lname = local_name(child.tag)
        if lname == "element":
            yield child
        elif lname in ("complexContent", "extension", "sequence", "all", "choice", "restriction"):
            yield from _property_elements(child)


def parse_describe_feature_type(content: bytes, type_name: str) -> tuple[list[str], list[str]]:
    """Liefert (Sachdatenfelder, Geometriefelder) des Feature-Typs."""
    raise_for_exception(content)
    root = _parse_xml(content)
    wanted = type_name.split(":")[-1]
    top_elements = [e for e in root if local_name(e.tag) == "element"]
    target = next((e for e in top_elements if e.get("name") == wanted), None)
    if target is None:
        raise WfsError(f"DescribeFeatureType enthält kein Element {wanted!r}")
    ctype = next((c for c in target if local_name(c.tag) == "complexType"), None)
    if ctype is None and target.get("type"):
        type_local = target.get("type").split(":")[-1]
        ctype = next(
            (c for c in root if local_name(c.tag) == "complexType" and c.get("name") == type_local),
            None,
        )
    if ctype is None:
        raise WfsError(f"Typdefinition für {wanted!r} nicht gefunden")
    fields: list[str] = []
    geometries: list[str] = []
    for el in _property_elements(ctype):
        name = el.get("name") or (el.get("ref") or "").split(":")[-1]
        if not name:
            continue
        (geometries if _is_geometry_type(el.get("type") or "") else fields).append(name)
    return fields, geometries


# ------------------------------------------------------------------ GetFeature-Antworten


@dataclass
class Page:
    records: list[RawRecord]
    number_matched: int | None = None
    number_returned: int | None = None
    has_next: bool = False


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _simple_value(elem: Any) -> Any:
    if elem.get(XSI_NIL) in ("true", "1"):
        return None
    if len(elem) == 0:
        text = (elem.text or "").strip()
        if text:
            return text
        return elem.get(XLINK_HREF) or None
    nested: dict[str, Any] = {}
    for child in elem:
        key = local_name(child.tag)
        value = _simple_value(child)
        if key in nested:
            prev = nested[key]
            nested[key] = [*prev, value] if isinstance(prev, list) else [prev, value]
        else:
            nested[key] = value
    return nested


def _feature_record(feature: Any, default_srs: str | None, geometry_field: str | None) -> RawRecord:
    fid = feature.get("fid")
    for attr, value in feature.attrib.items():
        if local_name(attr) == "id" and namespace(attr).startswith("http://www.opengis.net/gml"):
            fid = value
    props: dict[str, Any] = {}
    geom = None
    srs = None
    geom_error = None
    for child in feature:
        if is_gml(child):
            continue  # gml:boundedBy, gml:name, gml:description ...
        name = local_name(child.tag)
        g_elem = find_geometry_element(child)
        if g_elem is not None:
            wanted = geometry_field is None or name == geometry_field
            if wanted and geom is None and geom_error is None:
                try:
                    geom, srs = parse_gml_geometry(g_elem, default_srs)
                except (GeometryError, UnknownCrsError) as exc:
                    geom_error = str(exc)
            continue
        value = _simple_value(child)
        if name in props:
            prev = props[name]
            props[name] = [*prev, value] if isinstance(prev, list) else [prev, value]
        else:
            props[name] = value
    srid = None
    if geom is not None:
        try:
            srid = srid_from_crs(srs) if srs else None
        except UnknownCrsError as exc:
            geom, geom_error = None, str(exc)
        if srid is None and geom is not None:
            geom, geom_error = None, "Geometrie ohne srsName und ohne Vorgabe (source_crs)"
    return RawRecord(props, geom, srid, fid, ref=fid, geometry_error=geom_error)


def parse_gml_page(
    content: bytes, default_srs: str | None = None, geometry_field: str | None = None
) -> Page:
    """Liest eine wfs:FeatureCollection (WFS 2.0 oder 1.1.0) speicherschonend ein."""
    raise_for_exception(content)
    records: list[RawRecord] = []
    page = Page(records)
    depth = 0
    root = None
    try:
        for event, elem in SafeET.iterparse(io.BytesIO(content), events=("start", "end")):
            if event == "start":
                depth += 1
                if root is None:
                    root = elem
                    if local_name(elem.tag) != "FeatureCollection":
                        raise WfsError(
                            f"Unerwartete Antwort auf GetFeature: <{local_name(elem.tag)}>"
                        )
                    page.number_matched = _to_int(elem.get("numberMatched"))
                    if page.number_matched is None:
                        page.number_matched = _to_int(elem.get("numberOfFeatures"))
                    page.number_returned = _to_int(elem.get("numberReturned"))
                    page.has_next = bool(elem.get("next"))
                continue
            if depth == 2:
                lname = local_name(elem.tag)
                if lname in ("member", "featureMember", "featureMembers"):
                    for child in elem:
                        if local_name(child.tag) == "FeatureCollection":
                            for member in child:
                                for feature in member:
                                    records.append(
                                        _feature_record(feature, default_srs, geometry_field)
                                    )
                        else:
                            records.append(_feature_record(child, default_srs, geometry_field))
                    elem.clear()
                elif lname == "truncatedResponse":
                    raise WfsError("Server meldet eine abgeschnittene Antwort (truncatedResponse)")
            depth -= 1
    except (SafeET.ParseError, ValueError) as exc:
        if isinstance(exc, WfsError):
            raise
        raise WfsError(f"GML-Antwort ist kein gültiges XML: {exc}") from exc
    return page


def parse_geojson_page(content: bytes, srid_override: int | None = None) -> Page:
    """Liest eine GeoJSON-FeatureCollection. Ohne ``crs``-Angabe gilt EPSG:4326 (RFC 7946)."""
    raise_for_exception(content)
    try:
        obj = json.loads(content)
    except ValueError as exc:
        raise WfsError(f"Antwort ist kein gültiges GeoJSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise WfsError("GeoJSON-Antwort ist kein Objekt")
    if obj.get("type") == "Feature":
        features = [obj]
    elif obj.get("type") == "FeatureCollection":
        features = obj.get("features") or []  # manche Server lassen leere Listen weg
    else:
        raise WfsError(f"Unerwarteter GeoJSON-Typ {obj.get('type')!r}")
    srid = srid_override
    if srid is None:
        crs = obj.get("crs")
        name = crs.get("properties", {}).get("name") if isinstance(crs, dict) else None
        srid = srid_from_crs(name) if name else 4326
    records: list[RawRecord] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = None
        error = None
        if feat.get("geometry"):
            try:
                geom = shape(feat["geometry"])
            except (ValueError, TypeError, KeyError, AttributeError, GEOSException) as exc:
                error = f"GeoJSON-Geometrie nicht lesbar: {exc}"
        props = feat.get("properties") or {}
        fid = feat.get("id")
        fid = str(fid) if fid is not None else None
        records.append(RawRecord(dict(props), geom, srid, fid, ref=fid, geometry_error=error))
    matched = obj.get("numberMatched", obj.get("totalFeatures"))
    return Page(
        records,
        number_matched=matched if isinstance(matched, int) else None,
        number_returned=_to_int(obj.get("numberReturned")),
    )


# ------------------------------------------------------------------ Adapter


def _fmt_number(value: float) -> str:
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    return text or "0"


@register
class WfsAdapter(SourceAdapter):
    source_type = "wfs"
    config: WfsSource

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._declared: list[str] | None = None

    def declared_fields(self) -> list[str] | None:
        return self._declared

    # -- Anfragen ---------------------------------------------------------

    def _get(self, url: str, params: dict[str, Any]) -> bytes:
        try:
            response = self.http.get(url, params=params)
        except HttpError as exc:
            raise_for_exception(exc.body)  # liefert die Fehlermeldung des Dienstes, falls vorhanden
            raise
        return response.content

    def _capabilities(self) -> Capabilities:
        cfg = self.config
        base = {"SERVICE": "WFS", "REQUEST": "GetCapabilities"}
        if cfg.version == "1.1.0":
            return parse_capabilities(self._get(cfg.url, {**base, "VERSION": "1.1.0"}))
        accept = "2.0.0" if cfg.version == "2.0.0" else "2.0.0,1.1.0"
        try:
            return parse_capabilities(self._get(cfg.url, {**base, "ACCEPTVERSIONS": accept}))
        except (WfsError, HttpError) as exc:
            if cfg.version != "auto":
                raise
            log.info("%s: GetCapabilities 2.0.0 fehlgeschlagen (%s), versuche 1.1.0", cfg.id, exc)
            try:
                return parse_capabilities(self._get(cfg.url, {**base, "VERSION": "1.1.0"}))
            except (WfsError, HttpError) as fallback:
                # Ursache der ersten Anfrage melden, den Rückfall nur ergänzend
                message = f"{exc} (Rückfall auf WFS 1.1.0 ebenfalls fehlgeschlagen: {fallback})"
                if isinstance(exc, HttpError):
                    raise HttpError(message, exc.status_code, exc.body) from fallback
                raise WfsError(message) from fallback

    def _namespaces_param(self, version: str) -> dict[str, str]:
        ns = self.config.namespaces
        if not ns:
            return {}
        if version == "2.0.0":
            return {"NAMESPACES": ",".join(f"xmlns({p},{uri})" for p, uri in ns.items())}
        return {"NAMESPACE": ",".join(f"xmlns({p}={uri})" for p, uri in ns.items())}

    def _describe(self, version: str) -> None:
        cfg = self.config
        type_param = "TYPENAMES" if version == "2.0.0" else "TYPENAME"
        params = {
            "SERVICE": "WFS",
            "VERSION": version,
            "REQUEST": "DescribeFeatureType",
            type_param: cfg.type_name,
            **self._namespaces_param(version),
        }
        try:
            fields, geometries = parse_describe_feature_type(
                self._get(cfg.url, params), cfg.type_name
            )
        except (WfsError, HttpError) as exc:
            self.warnings.add("schema_unavailable", example=str(exc)[:300])
            return
        self._declared = fields
        self.details["geometry_fields"] = geometries
        if cfg.geometry_field and geometries and cfg.geometry_field not in geometries:
            self.warnings.add(
                "schema_mapped_field_missing",
                example=f"{cfg.geometry_field} (Geometrie; vorhanden: {', '.join(geometries)})",
            )

    def _choose_format(self, caps: Capabilities, ft: FeatureTypeInfo, version: str):
        want = self.config.output_format
        offered = list(dict.fromkeys([*caps.output_formats, *ft.output_formats]))
        if want not in ("auto", "geojson", "gml"):
            return want, ("geojson" if "json" in want.lower() else "gml")
        if want in ("auto", "geojson"):
            for fmt in offered:
                if "json" in fmt.lower():
                    return fmt, "geojson"
            if want == "geojson":
                raise WfsError("Der Dienst bietet kein GeoJSON-Ausgabeformat an")
        if version == "2.0.0":
            for fmt in GML32_FORMATS:
                if fmt in offered:
                    return fmt, "gml"
        return None, "gml"

    def _base_params(self, version: str, fmt: str | None, srs: str | None) -> dict[str, Any]:
        cfg = self.config
        params: dict[str, Any] = {"SERVICE": "WFS", "VERSION": version, "REQUEST": "GetFeature"}
        params["TYPENAMES" if version == "2.0.0" else "TYPENAME"] = cfg.type_name
        params.update(self._namespaces_param(version))
        if fmt:
            params["OUTPUTFORMAT"] = fmt
        if srs:
            params["SRSNAME"] = srs
        if cfg.sort_by:
            params["SORTBY"] = cfg.sort_by
        if cfg.bbox:
            bbox = ",".join(_fmt_number(v) for v in cfg.bbox)
            crs = cfg.bbox_crs or srs
            params["BBOX"] = f"{bbox},{crs}" if crs else bbox
        if cfg.filter:
            params["FILTER"] = " ".join(cfg.filter.split())
        params.update(cfg.extra_params)
        return params

    # -- Lesen ------------------------------------------------------------

    def read(self) -> Iterator[RawRecord]:
        cfg = self.config
        caps = self._capabilities()
        version = caps.version if cfg.version == "auto" else cfg.version
        if version.startswith("2."):
            version = "2.0.0"
        elif version.startswith("1.1"):
            version = "1.1.0"
        else:
            raise WfsError(f"WFS-Version {caps.version or '?'} wird nicht unterstützt")
        ft = caps.find_feature_type(cfg.type_name)
        if ft is None:
            known = ", ".join(f.name for f in caps.feature_types[:15])
            raise WfsError(f"Feature-Typ {cfg.type_name!r} nicht im Dienst vorhanden ({known})")
        self._describe(version)
        fmt, kind = self._choose_format(caps, ft, version)
        srs = cfg.srs_name or (ft.default_crs if kind == "gml" else None)
        srid_override = srid_from_crs(cfg.source_crs) if cfg.source_crs else None
        default_srs = cfg.source_crs or srs or ft.default_crs
        url = cfg.url
        if cfg.use_advertised_urls and caps.get_feature_url:
            url = caps.get_feature_url

        page_size = cfg.page_size
        if caps.count_default and caps.count_default < page_size:
            page_size = caps.count_default
            self.warnings.add(
                "page_size_limited", example=f"{cfg.page_size} -> {caps.count_default}"
            )
        if cfg.paging == "always":
            paging = True
        elif cfg.paging == "never":
            paging = False
        else:  # auto: Angabe ImplementsResultPaging der Capabilities
            paging = version == "2.0.0" and caps.implements_paging is not False
        self.details.update(
            {
                "wfs_version": version,
                "output_format": fmt or "(Standard des Servers, GML)",
                "format_kind": kind,
                "srs_name": srs,
                "count_default": caps.count_default,
                "paging": paging,
                "server_implements_paging": caps.implements_paging,
                "page_size": page_size if paging else None,
            }
        )
        params = self._base_params(version, fmt, srs)
        start = 0
        total = 0
        pages = 0
        previous_signature = None
        while True:
            count = page_size
            if cfg.max_features is not None:
                count = min(count, cfg.max_features - total)
            request = dict(params)
            if paging:
                request["STARTINDEX"] = start
                request["COUNT" if version == "2.0.0" else "MAXFEATURES"] = count
            elif version == "2.0.0":
                if cfg.max_features:
                    request["COUNT"] = cfg.max_features
            elif cfg.max_features:
                request["MAXFEATURES"] = cfg.max_features
            content = self._get(url, request)
            if kind == "geojson":
                page = parse_geojson_page(content, srid_override)
            else:
                page = parse_gml_page(content, default_srs, cfg.geometry_field)
            pages += 1
            n = len(page.records)
            if n and paging and pages > 1:
                first = page.records[0]
                signature = first.feature_id or json.dumps(
                    first.properties, sort_keys=True, default=str
                )
                if signature == previous_signature:
                    raise WfsError(
                        "Der Dienst ignoriert STARTINDEX (gleiche Seite wiederholt); "
                        "Paginierung nicht möglich"
                    )
            if n:
                first = page.records[0]
                previous_signature = first.feature_id or json.dumps(
                    first.properties, sort_keys=True, default=str
                )
            yield from page.records
            total += n
            if not paging:
                if not cfg.max_features and caps.count_default and n >= caps.count_default:
                    raise WfsError(
                        f"Antwort möglicherweise vom Server gekürzt ({n} Objekte = CountDefault); "
                        f"ohne vollständigen Abruf wird nichts ersetzt. Paginierung aktivieren "
                        f"(paging: always)."
                    )
                matched = page.number_matched
                if not cfg.max_features and matched is not None and n < matched:
                    raise WfsError(
                        f"Antwort vom Server gekürzt ({n} von {matched} Objekten); ohne "
                        f"vollständigen Abruf wird nichts ersetzt. Paginierung aktivieren "
                        f"(paging: always)."
                    )
                if cfg.max_features is not None and total >= cfg.max_features:
                    self.complete = False
                break
            if n == 0:
                break
            if cfg.max_features is not None and total >= cfg.max_features:
                self.complete = False
                break
            if page.number_matched is not None:
                if start + n >= page.number_matched:
                    break
            elif n < count and not page.has_next:
                break
            start += n
            if pages >= MAX_PAGES:
                raise WfsError(f"Abbruch nach {MAX_PAGES} Seiten")
        self.details["pages"] = pages
        self.details["features_received"] = total
