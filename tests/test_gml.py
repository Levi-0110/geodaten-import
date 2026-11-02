from __future__ import annotations

import pytest
from defusedxml import ElementTree as SafeET

from geoimport.geometry import (
    GeometryError,
    needs_axis_swap,
    parse_gml_geometry,
    srid_from_crs,
)
from geoimport.sources.wfs import WfsError, parse_geojson_page, parse_gml_page

GML32 = 'xmlns:gml="http://www.opengis.net/gml/3.2"'
GML31 = 'xmlns:gml="http://www.opengis.net/gml"'


def geom(xml: str, default_srs=None):
    return parse_gml_geometry(SafeET.fromstring(xml), default_srs)


@pytest.mark.parametrize(
    ("srs", "epsg", "swap"),
    [
        ("urn:ogc:def:crs:EPSG::25833", 25833, False),
        ("urn:ogc:def:crs:EPSG::4326", 4326, True),
        ("urn:ogc:def:crs:EPSG:6.6:4258", 4258, True),
        ("http://www.opengis.net/def/crs/EPSG/0/4326", 4326, True),
        ("http://www.opengis.net/gml/srs/epsg.xml#4326", 4326, False),
        ("EPSG:4326", 4326, False),
        ("urn:ogc:def:crs:OGC:1.3:CRS84", 4326, False),
        ("urn:ogc:def:crs:EPSG::31468", 31468, True),
    ],
)
def test_crs_and_axis_order(srs, epsg, swap):
    assert srid_from_crs(srs) == epsg
    assert needs_axis_swap(srs) is swap


def test_point_lat_lon_is_swapped_for_urn_4326():
    g, srs = geom(
        f'<gml:Point {GML32} srsName="urn:ogc:def:crs:EPSG::4326">'
        "<gml:pos>52.4125 12.5316</gml:pos></gml:Point>"
    )
    assert (g.x, g.y) == (12.5316, 52.4125)
    assert srs == "urn:ogc:def:crs:EPSG::4326"


def test_polygon_with_hole_and_3d_poslist():
    g, _ = geom(
        f'<gml:Polygon {GML32} srsName="EPSG:25833" srsDimension="3">'
        "<gml:exterior><gml:LinearRing><gml:posList>"
        "0 0 1 10 0 1 10 10 1 0 10 1 0 0 1</gml:posList></gml:LinearRing></gml:exterior>"
        "<gml:interior><gml:LinearRing><gml:posList>"
        "2 2 1 4 2 1 4 4 1 2 4 1 2 2 1</gml:posList></gml:LinearRing></gml:interior>"
        "</gml:Polygon>"
    )
    assert g.geom_type == "Polygon" and g.area == 96 and not g.has_z


def test_gml2_coordinates_and_multilinestring():
    g, _ = geom(
        f'<gml:MultiLineString {GML31} srsName="EPSG:25833"><gml:lineStringMember>'
        '<gml:LineString><gml:coordinates decimal="." cs="," ts=" ">0,0 3,4</gml:coordinates>'
        "</gml:LineString></gml:lineStringMember></gml:MultiLineString>"
    )
    assert g.geom_type == "MultiLineString" and g.length == 5


def test_curve_with_segments_and_surface_with_patches():
    curve, _ = geom(
        f"<gml:Curve {GML32}><gml:segments>"
        "<gml:LineStringSegment><gml:posList>0 0 1 0</gml:posList></gml:LineStringSegment>"
        "<gml:LineStringSegment><gml:posList>1 0 1 1</gml:posList></gml:LineStringSegment>"
        "</gml:segments></gml:Curve>"
    )
    assert list(curve.coords) == [(0, 0), (1, 0), (1, 1)]
    surface, _ = geom(
        f"<gml:Surface {GML32}><gml:patches><gml:PolygonPatch><gml:exterior><gml:LinearRing>"
        "<gml:posList>0 0 1 0 1 1 0 0</gml:posList></gml:LinearRing></gml:exterior>"
        "</gml:PolygonPatch></gml:patches></gml:Surface>"
    )
    assert surface.geom_type == "Polygon" and surface.area == 0.5


def test_unsupported_or_broken_geometry_raises():
    with pytest.raises(GeometryError):
        geom(
            f"<gml:Polygon {GML32}><gml:exterior><gml:LinearRing><gml:posList>0 0 1 1"
            "</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon>"
        )
    with pytest.raises(GeometryError):
        geom(f"<gml:Point {GML32}><gml:pos>abc def</gml:pos></gml:Point>")


def test_recorded_lgb_page_gml32(fixtures_dir):
    page = parse_gml_page(
        (fixtures_dir / "wfs/lgb_vg/getfeature_otl_start0_count3.xml").read_bytes()
    )
    assert page.number_matched is None  # deegree meldet numberMatched="unknown"
    assert [r.properties["otl_name"] for r in page.records] == ["Gollwitz", "Göttin", "Kirchmöser"]
    first = page.records[0]
    assert first.feature_id == "otl_452" and first.srid == 25833
    assert first.geometry.geom_type == "MultiPolygon" and first.geometry.is_valid
    assert 12.0e6 < first.geometry.area < 12.1e6  # Gollwitz rund 12 km²
    assert "geom" not in first.properties and "boundedBy" not in first.properties
    assert first.properties["otl_aktualitaet"] == "2026-07-02"


def test_recorded_lgb_page_wfs110_gml311(fixtures_dir):
    page = parse_gml_page(
        (fixtures_dir / "wfs/lgb_vg/getfeature_otl_v110_maxfeatures2.xml").read_bytes()
    )
    assert [r.properties["otls"] for r in page.records] == ["120510000060", "120510000075"]
    assert all(r.srid == 25833 and r.geometry.geom_type == "MultiPolygon" for r in page.records)


def test_recorded_lfu_geojson_pages(fixtures_dir):
    base = fixtures_dir / "wfs/lfu_schutzg"
    page = parse_geojson_page((base / "getfeature_nsg_start0_count4.json").read_bytes())
    assert len(page.records) == 4
    rec = page.records[0]
    assert rec.srid == 4326 and rec.feature_id == "nsg_3141"
    assert rec.properties["Gebietsname"] == "Große Freiheit bei Plaue"
    assert 12.3 < rec.geometry.centroid.x < 12.5 and 52.3 < rec.geometry.centroid.y < 52.5
    # leere Seite des Dienstes enthält kein "features"-Element
    empty = parse_geojson_page((base / "getfeature_nsg_start12_count4.json").read_bytes())
    assert empty.records == []


def test_geojson_legacy_crs_member():
    content = (
        b'{"type":"FeatureCollection","crs":{"type":"name","properties":'
        b'{"name":"urn:ogc:def:crs:EPSG::25833"}},"features":[{"type":"Feature","id":1,'
        b'"geometry":{"type":"Point","coordinates":[368000,5808000]},"properties":{"a":1}}]}'
    )
    page = parse_geojson_page(content)
    assert page.records[0].srid == 25833 and page.records[0].feature_id == "1"


def test_exception_report_is_raised():
    content = (
        b'<?xml version="1.0"?><ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows/1.1">'
        b'<ows:Exception exceptionCode="InvalidParameterValue" locator="typeNames">'
        b"<ows:ExceptionText>Unbekannter Typ</ows:ExceptionText></ows:Exception>"
        b"</ows:ExceptionReport>"
    )
    with pytest.raises(WfsError, match="InvalidParameterValue .typeNames.: Unbekannter Typ"):
        parse_gml_page(content)


def test_xml_entities_are_rejected():
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]>'
        b'<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0">&b;'
        b"</wfs:FeatureCollection>"
    )
    with pytest.raises(WfsError):
        parse_gml_page(bomb)
