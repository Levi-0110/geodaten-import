from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from geoimport.config import HttpSettings, parse_source
from geoimport.http import HttpClient
from geoimport.mapping import FeatureStream
from geoimport.models import Warnings
from geoimport.sources import create_adapter
from geoimport.sources.wfs import WfsError, parse_capabilities, parse_describe_feature_type

PROJECT_ROOT = Path(__file__).parents[1]


def shipped_source(name: str, url: str, **overrides):
    data = yaml.safe_load((PROJECT_ROOT / "config" / "sources" / name).read_text("utf-8"))
    data.update(url=url, **overrides)
    return parse_source(data, name, PROJECT_ROOT / "config" / "sources")


def read_all(source, http):
    warnings = Warnings()
    adapter = create_adapter(source, http, warnings)
    records = list(adapter.read())
    return adapter, records, warnings


def test_capabilities_of_recorded_services(fixtures_dir):
    lgb = parse_capabilities((fixtures_dir / "wfs/lgb_vg/capabilities_2_0_0.xml").read_bytes())
    assert lgb.version == "2.0.0" and lgb.count_default == 300000
    assert lgb.implements_paging is False  # so angegeben, Paging funktioniert dennoch
    assert not any("json" in f for f in lgb.output_formats)
    otl = lgb.find_feature_type("app:otl")
    assert otl.title == "Ortsteile" and otl.default_crs == "urn:ogc:def:crs:EPSG::25833"
    lfu = parse_capabilities((fixtures_dir / "wfs/lfu_schutzg/capabilities_2_0_0.xml").read_bytes())
    assert "application/geo+json" in lfu.output_formats
    assert lfu.find_feature_type("nsg").name == "app:nsg"


def test_describe_feature_type(fixtures_dir):
    fields, geoms = parse_describe_feature_type(
        (fixtures_dir / "wfs/lgb_vg/describefeaturetype_otl.xml").read_bytes(), "app:otl"
    )
    assert fields[:4] == ["otls", "ags", "otls_kurz", "otl_name"] and geoms == ["geom"]


def test_wfs_gml_paging_against_recorded_responses(responder, http_client):
    src = shipped_source("lgb_ortsteile_brandenburg_havel.yml", "http://fixtures.test/wfs/lgb_vg")
    adapter, records, warnings = read_all(src, http_client)
    requests = [q for q in responder.requests_for("/wfs/lgb_vg") if q["REQUEST"] == "GetFeature"]
    assert [(q["STARTINDEX"], q["COUNT"]) for q in requests] == [("0", "3"), ("3", "3"), ("6", "3")]
    assert requests[0]["SRSNAME"] == "urn:ogc:def:crs:EPSG::25833"
    assert requests[0]["OUTPUTFORMAT"] == "application/gml+xml; version=3.2"
    assert "app:ags" in requests[0]["FILTER"] and requests[0]["SORTBY"] == "app:otls"
    assert requests[0]["NAMESPACES"] == "xmlns(app,http://www.deegree.org/app)"
    assert len(records) == 8 and len({r.feature_id for r in records}) == 8
    assert adapter.details["pages"] == 3 and adapter.details["format_kind"] == "gml"
    assert "otl_name" in adapter.declared_fields()
    features = list(FeatureStream(records, src, warnings))
    assert sorted(f.attributes["name"] for f in features)[:2] == ["Gollwitz", "Göttin"]
    assert len(warnings) == 0 and adapter.complete


def test_wfs_geojson_paging_and_duplicate_merge(responder, http_client):
    src = shipped_source("lfu_naturschutzgebiete.yml", "http://fixtures.test/wfs/lfu_schutzg")
    adapter, records, warnings = read_all(src, http_client)
    requests = [
        q for q in responder.requests_for("/wfs/lfu_schutzg") if q["REQUEST"] == "GetFeature"
    ]
    assert [(q["STARTINDEX"], q["COUNT"]) for q in requests] == [("0", "4"), ("4", "4"), ("8", "4")]
    assert requests[0]["OUTPUTFORMAT"] == "application/geo+json"
    assert requests[0]["BBOX"] == "321000,5796800,345200,5815700,urn:ogc:def:crs:EPSG::25833"
    assert "SRSNAME" not in requests[0]
    assert len(records) == 10 and all(r.srid == 4326 for r in records)
    features = list(FeatureStream(records, src, warnings))
    keys = [f.key for f in features]
    # Gebiete aus zwei Teilflächen: werden später in der Datenbank zusammengefasst
    assert len(keys) == 10 and len(set(keys)) == 8
    assert sorted(k for k in set(keys) if keys.count(k) == 2) == ["3540-501", "3641-507"]
    plaue = next(f for f in features if f.key == "3540-501")
    assert plaue.attributes["name"] == "Große Freiheit bei Plaue"
    assert len(warnings) == 0


def _synthetic_wfs(features: int, count_default: int, ignore_start: bool = False):
    caps = f"""<?xml version="1.0"?>
<wfs:WFS_Capabilities version="2.0.0" xmlns:wfs="http://www.opengis.net/wfs/2.0"
  xmlns:ows="http://www.opengis.net/ows/1.1">
 <ows:OperationsMetadata>
  <ows:Operation name="GetFeature"><ows:Parameter name="outputFormat"><ows:AllowedValues>
   <ows:Value>application/json</ows:Value></ows:AllowedValues></ows:Parameter></ows:Operation>
  <ows:Constraint name="ImplementsResultPaging"><ows:NoValues/>
   <ows:DefaultValue>TRUE</ows:DefaultValue></ows:Constraint>
  <ows:Constraint name="CountDefault"><ows:NoValues/>
   <ows:DefaultValue>{count_default}</ows:DefaultValue></ows:Constraint>
 </ows:OperationsMetadata>
 <wfs:FeatureTypeList><wfs:FeatureType><wfs:Name>t:punkte</wfs:Name>
  <wfs:DefaultCRS>urn:ogc:def:crs:EPSG::25833</wfs:DefaultCRS></wfs:FeatureType>
 </wfs:FeatureTypeList>
</wfs:WFS_Capabilities>"""
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        seen.append(q)
        if q["REQUEST"] == "GetCapabilities":
            return httpx.Response(200, content=caps.encode())
        if q["REQUEST"] == "DescribeFeatureType":
            return httpx.Response(404)
        start = 0 if ignore_start else int(q.get("STARTINDEX", 0))
        count = min(int(q.get("COUNT", count_default)), count_default)
        items = [
            {
                "type": "Feature",
                "id": f"p{i}",
                "properties": {"nr": i},
                "geometry": {"type": "Point", "coordinates": [368000 + i, 5808000]},
            }
            for i in range(start, min(start + count, features))
        ]
        body = {
            "type": "FeatureCollection",
            "numberMatched": features,
            "numberReturned": len(items),
            "features": items,
        }
        return httpx.Response(200, content=json.dumps(body).encode())

    client = HttpClient(HttpSettings(retries=0), transport=httpx.MockTransport(handler))
    return client, seen


def synthetic_source(**overrides):
    data = {
        "id": "synthetisch",
        "type": "wfs",
        "url": "http://synthetic.test/wfs",
        "type_name": "t:punkte",
        "key_field": "nr",
        "page_size": 100,
        "target": {"table": "ziel", "exclusive": True, "geometry_column": "geom"},
        "metadata": {"provider": "Test", "licence": "CC0-1.0"},
        **overrides,
    }
    return parse_source(data, "test", Path.cwd())


def test_count_default_limits_page_size():
    client, seen = _synthetic_wfs(features=5, count_default=2)
    adapter, records, warnings = read_all(synthetic_source(), client)
    pages = [(q["STARTINDEX"], q["COUNT"]) for q in seen if q["REQUEST"] == "GetFeature"]
    assert pages == [("0", "2"), ("2", "2"), ("4", "2")]
    assert [r.properties["nr"] for r in records] == [0, 1, 2, 3, 4]
    assert "page_size_limited" in warnings.codes() and "schema_unavailable" in warnings.codes()
    assert adapter.details["count_default"] == 2


def test_server_ignoring_startindex_is_detected():
    client, _ = _synthetic_wfs(features=5, count_default=2, ignore_start=True)
    with pytest.raises(WfsError, match="STARTINDEX"):
        read_all(synthetic_source(), client)


def test_max_features_stops_paging():
    client, seen = _synthetic_wfs(features=50, count_default=10)
    adapter, records, _ = read_all(synthetic_source(max_features=15), client)
    assert len(records) == 15
    assert [q["COUNT"] for q in seen if q["REQUEST"] == "GetFeature"] == ["10", "5"]
    # bewusst begrenzter Abruf: gilt als unvollständig, die Pipeline ersetzt dann nichts
    assert adapter.complete is False


def test_truncated_answer_without_paging_is_an_error():
    client, _ = _synthetic_wfs(features=5, count_default=2)
    with pytest.raises(WfsError, match="gekürzt"):
        read_all(synthetic_source(paging="never"), client)
    client, _ = _synthetic_wfs(features=5, count_default=100)
    with pytest.raises(WfsError, match=r"gekürzt \(3 von 5 Objekten\)"):
        read_all(synthetic_source(paging="never", extra_params={"COUNT": "3"}), client)


def test_fallback_to_wfs_110(responder):
    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        if "ACCEPTVERSIONS" in q:  # Dienst ohne WFS 2.0
            return httpx.Response(400, content=b"<html>Bad Request</html>")
        status, ctype, body = responder.respond(request.url.path, q)
        return httpx.Response(status, headers={"Content-Type": ctype}, content=body)

    client = HttpClient(HttpSettings(retries=0), transport=httpx.MockTransport(handler))
    src = shipped_source(
        "lgb_ortsteile_brandenburg_havel.yml",
        "http://fixtures.test/wfs/lgb_vg",
        max_features=2,
        filter=None,
        paging="auto",
    )
    adapter, records, _ = read_all(src, client)
    getfeature = [q for _, q in responder.requests if q.get("REQUEST") == "GetFeature"]
    assert getfeature[0]["VERSION"] == "1.1.0" and getfeature[0]["MAXFEATURES"] == "2"
    assert "TYPENAME" in getfeature[0] and "NAMESPACE" in getfeature[0]
    assert adapter.details["wfs_version"] == "1.1.0" and not adapter.details["paging"]
    assert [r.properties["otl_name"] for r in records] == ["Gollwitz", "Göttin"]


def test_unknown_feature_type_lists_available_types(http_client):
    src = shipped_source(
        "lgb_ortsteile_brandenburg_havel.yml",
        "http://fixtures.test/wfs/lgb_vg",
        type_name="app:gibtsnicht",
    )
    with pytest.raises(WfsError, match="app:gibtsnicht.*app:otl"):
        read_all(src, http_client)


def test_service_exception_is_reported(http_client):
    src = shipped_source(
        "lgb_ortsteile_brandenburg_havel.yml", "http://fixtures.test/wfs/lgb_vg", page_size=7
    )
    with pytest.raises(WfsError, match="Keine aufgezeichnete Antwort"):
        read_all(src, http_client)
