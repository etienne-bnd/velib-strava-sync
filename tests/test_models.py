# -*- coding: utf-8 -*-
"""Tests des structures de données."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from models import Station, VelibTrip


def test_start_time_naif_devient_utc() -> None:
    """Une date sans fuseau est interprétée comme de l'UTC."""
    trip = VelibTrip("a", datetime(2026, 9, 5, 7, 0), 600)
    assert trip.start_time.tzinfo is timezone.utc


def test_start_time_est_converti_en_utc() -> None:
    """Une date dans un autre fuseau est convertie, pas seulement étiquetée."""
    from zoneinfo import ZoneInfo

    trip = VelibTrip("a", datetime(2026, 9, 5, 9, 0, tzinfo=ZoneInfo("Europe/Paris")), 600)
    assert trip.start_time.hour == 7  # UTC+2 en été


@pytest.mark.parametrize(
    ("depart", "arrivee", "duree", "attendu"),
    [
        ("100", "100", 60, True),    # boucle courte : annulation
        ("100", "100", 120, True),   # exactement deux minutes : annulation
        ("100", "100", 121, False),  # boucle plus longue : vrai trajet
        ("100", "200", 60, False),   # stations différentes : vrai trajet
        (None, None, 60, False),     # stations inconnues : on ne conclut pas
    ],
)
def test_detection_des_annulations(depart, arrivee, duree, attendu) -> None:
    """Une boucle de deux minutes ou moins est une annulation."""
    trip = VelibTrip("a", datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc), duree, depart, arrivee)
    assert trip.is_probable_cancellation is attendu


@pytest.mark.parametrize(
    ("heure", "fragment"),
    [(7, "matinal"), (12, "de midi"), (16, "après-midi"), (20, "du soir"), (2, "nocturne")],
)
def test_nom_par_defaut_selon_le_moment(heure, fragment) -> None:
    """Le nom par défaut reflète le moment de la journée."""
    trip = VelibTrip("a", datetime(2026, 9, 5, heure, 0, tzinfo=timezone.utc), 600)
    assert fragment in trip.default_name()


def test_coordonnees_de_station_en_lon_lat() -> None:
    """`Station.coordinates` respecte l'ordre (longitude, latitude)."""
    station = Station("1", "Test", longitude=2.35, latitude=48.85)
    assert station.coordinates == (2.35, 48.85)
