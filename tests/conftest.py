# -*- coding: utf-8 -*-
"""Fixtures partagées et ajout de la racine du projet au chemin d'import."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from models import Station, VelibTrip  # noqa: E402


@pytest.fixture
def trip() -> VelibTrip:
    """Un trajet Vélib' nominal : Benjamin Godard -> Tour Eiffel, 15 min."""
    return VelibTrip(
        trip_id="trajet-1",
        start_time=datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc),
        duration_seconds=900,
        departure_station_id="16107",
        arrival_station_id="07001",
        departure_station_name="Benjamin Godard",
        arrival_station_name="Tour Eiffel",
    )


@pytest.fixture
def station_payload() -> dict:
    """Charge utile GBFS minimale, au format de l'open data Smovengo."""
    return {
        "data": {
            "stations": [
                {
                    "station_id": 213688169,
                    "stationCode": "16107",
                    "name": "Benjamin Godard - Victor Hugo",
                    "lat": 48.865983,
                    "lon": 2.275725,
                },
                {
                    "station_id": 85008247,
                    "stationCode": "07001",
                    "name": "Tour Eiffel",
                    "lat": 48.858370,
                    "lon": 2.294480,
                },
            ]
        }
    }


@pytest.fixture
def catalog(station_payload: dict):
    """Catalogue de stations construit à partir de `station_payload`."""
    from routing import StationCatalog

    return StationCatalog.from_payload(station_payload)


@pytest.fixture
def paris_route() -> list[tuple[float, float]]:
    """Un itinéraire aux points volontairement très irrégulièrement espacés."""
    return [
        (2.275725, 48.865983),
        (2.275900, 48.865800),  # 25 m
        (2.290000, 48.860000),  # 1,2 km
        (2.294480, 48.858370),  # 380 m
    ]
