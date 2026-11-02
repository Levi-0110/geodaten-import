from __future__ import annotations

import datetime as dt
import decimal

from shapely.geometry import Point, Polygon

from geoimport.geometry import to_wkb
from geoimport.hashing import attribute_text, content_hash


def test_attribute_text_normalisation():
    assert attribute_text(None) is None
    assert attribute_text(True) == "true"
    assert attribute_text(8) == "8"
    assert attribute_text(511.38389833) == "511.38389833"
    assert attribute_text(float("nan")) is None
    assert attribute_text(decimal.Decimal("1.50")) == "1.5"  # Schreibweise egal
    assert attribute_text(decimal.Decimal("1E+3")) == "1000"
    assert attribute_text(dt.date(2026, 7, 2)) == "2026-07-02"
    assert attribute_text({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'
    assert attribute_text("Göttin") == "Göttin"


def test_hash_is_deterministic_and_independent_of_key_order():
    wkb = to_wkb(Point(13.05, 52.39))
    a = content_hash({"name": "A", "plz": "14467"}, wkb, 4326)
    b = content_hash({"plz": "14467", "name": "A"}, wkb, 4326)
    assert a == b
    assert len(a) == 64 and int(a, 16) >= 0


def test_hash_treats_equal_text_values_as_equal():
    wkb = to_wkb(Point(1, 2))
    assert content_hash({"n": 8}, wkb, 25833) == content_hash({"n": "8"}, wkb, 25833)


def test_hash_detects_attribute_geometry_and_crs_changes():
    point = Point(13.05369, 52.39517)
    base = content_hash({"status": "In Betrieb"}, to_wkb(point), 4326)
    assert base != content_hash({"status": "Außer Betrieb"}, to_wkb(point), 4326)
    assert base != content_hash({"status": "In Betrieb"}, to_wkb(Point(13.05370, 52.39517)), 4326)
    assert base != content_hash({"status": "In Betrieb"}, to_wkb(point), 4258)
    assert base != content_hash({"status": "In Betrieb", "neu": None}, to_wkb(point), 4326)


def test_hash_ignores_z_values():
    square = [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]
    square_z = [(x, y, 42.0) for x, y in square]
    assert to_wkb(Polygon(square)) == to_wkb(Polygon(square_z))
