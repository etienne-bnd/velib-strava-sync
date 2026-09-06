# -*- coding: utf-8 -*-
"""Tests de la génération du GPX horodaté."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import gpxpy
import pytest

import gpx_builder
from gpx_builder import GpxBuildError, build_gpx, compute_timestamps
from routing import haversine_distance

DEPART = datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc)


def test_horodatages_encadrent_exactement_la_duree(paris_route) -> None:
    """Le premier point est à l'heure de départ, le dernier à départ + durée."""
    timestamps = compute_timestamps(paris_route, DEPART, 900)
    assert timestamps[0] == DEPART
    assert timestamps[-1] == DEPART + timedelta(seconds=900)


def test_un_horodatage_par_point(paris_route) -> None:
    """Chaque point du tracé reçoit son propre horodatage."""
    assert len(compute_timestamps(paris_route, DEPART, 900)) == len(paris_route)


def test_horodatages_strictement_croissants(paris_route) -> None:
    """Un GPX dont le temps recule serait rejeté par Strava."""
    timestamps = compute_timestamps(paris_route, DEPART, 900)
    assert all(timestamps[i] < timestamps[i + 1] for i in range(len(timestamps) - 1))


def test_vitesse_constante_malgre_des_points_irreguliers(paris_route) -> None:
    """C'est le cœur du module : la vitesse doit être identique sur chaque segment.

    Les points de `paris_route` sont espacés de 25 m à 1,2 km. Une répartition
    du temps par indice donnerait des vitesses dans un rapport de 1 à 50 ;
    la répartition par distance doit les rendre identiques.
    """
    timestamps = compute_timestamps(paris_route, DEPART, 900)
    vitesses = [
        haversine_distance(paris_route[i], paris_route[i + 1])
        / (timestamps[i + 1] - timestamps[i]).total_seconds()
        for i in range(len(paris_route) - 1)
    ]
    # Tolérance 1e-6 : elle absorbe l'accumulation en virgule flottante de la
    # somme cumulée tout en restant 10 000 fois plus serrée que l'écart
    # qu'une répartition par indice produirait ici (facteur ~50).
    assert max(vitesses) == pytest.approx(min(vitesses), rel=1e-6)


def test_vitesse_moyenne_conforme_a_la_duree(paris_route) -> None:
    """La vitesse moyenne du tracé correspond à distance totale / durée."""
    duree = 900
    timestamps = compute_timestamps(paris_route, DEPART, duree)
    distance = gpx_builder.compute_cumulative_distances(paris_route)[-1]
    ecoule = (timestamps[-1] - timestamps[0]).total_seconds()
    assert distance / ecoule == pytest.approx(distance / duree)


def test_date_naive_traitee_comme_utc(paris_route) -> None:
    """Une date sans fuseau est étiquetée UTC, pas rejetée."""
    timestamps = compute_timestamps(paris_route, datetime(2026, 9, 5, 7, 0), 900)
    assert timestamps[0].tzinfo is timezone.utc


def test_conversion_depuis_un_autre_fuseau(paris_route) -> None:
    """Une date en heure de Paris est convertie en UTC."""
    from zoneinfo import ZoneInfo

    depart = datetime(2026, 9, 5, 9, 0, tzinfo=ZoneInfo("Europe/Paris"))
    assert compute_timestamps(paris_route, depart, 900)[0].hour == 7


def test_itineraire_de_longueur_nulle_reste_gerable() -> None:
    """Des points tous confondus : repli sur une répartition par indice."""
    points = [(2.35, 48.85)] * 4
    timestamps = compute_timestamps(points, DEPART, 300)
    assert timestamps[-1] - timestamps[0] == timedelta(seconds=300)
    assert timestamps[1] - timestamps[0] == timedelta(seconds=100)


@pytest.mark.parametrize("points", [[], [(2.35, 48.85)]])
def test_itineraire_trop_court_rejete(points) -> None:
    """Moins de deux points ne permet pas de construire une trace."""
    with pytest.raises(GpxBuildError, match="au moins deux points"):
        compute_timestamps(points, DEPART, 900)


@pytest.mark.parametrize("duree", [0, -60])
def test_duree_non_positive_rejetee(paris_route, duree) -> None:
    """Une durée nulle ou négative est une donnée invalide."""
    with pytest.raises(GpxBuildError, match="strictement positive"):
        compute_timestamps(paris_route, DEPART, duree)


# --------------------------------------------------------------------------- #
# Fichier produit
# --------------------------------------------------------------------------- #

def test_gpx_ecrit_et_relisible(paris_route, tmp_path) -> None:
    """Le fichier produit doit être un GPX valide, relisible par gpxpy."""
    chemin = build_gpx(paris_route, DEPART, 900, "Test", tmp_path / "trip.gpx")
    assert chemin.is_file()

    with chemin.open(encoding="utf-8") as handle:
        gpx = gpxpy.parse(handle)

    assert len(gpx.tracks) == 1
    assert gpx.tracks[0].name == "Test"
    points = gpx.tracks[0].segments[0].points
    assert len(points) == len(paris_route)
    assert all(point.time is not None for point in points)


def test_ordre_latitude_longitude_respecte(paris_route, tmp_path) -> None:
    """Le GPX inverse l'ordre des coordonnées : une inversion mettrait le
    trajet en mer, il faut donc le vérifier explicitement."""
    chemin = build_gpx(paris_route, DEPART, 900, "Test", tmp_path / "trip.gpx")
    with chemin.open(encoding="utf-8") as handle:
        premier = gpxpy.parse(handle).tracks[0].segments[0].points[0]

    longitude, latitude = paris_route[0]
    assert premier.latitude == pytest.approx(latitude)
    assert premier.longitude == pytest.approx(longitude)
    # Paris : latitude ~48,8 et longitude ~2,3. Une inversion se verrait ici.
    assert 48 < premier.latitude < 49
    assert 2 < premier.longitude < 3


def test_duree_du_gpx_conforme(paris_route, tmp_path) -> None:
    """gpxpy doit relire une durée égale à celle demandée."""
    chemin = build_gpx(paris_route, DEPART, 900, "Test", tmp_path / "trip.gpx")
    with chemin.open(encoding="utf-8") as handle:
        gpx = gpxpy.parse(handle)
    assert gpx.get_duration() == pytest.approx(900, abs=1)


def test_repertoire_parent_cree_au_besoin(paris_route, tmp_path) -> None:
    """Le module crée l'arborescence manquante plutôt que d'échouer."""
    chemin = build_gpx(paris_route, DEPART, 900, "Test", tmp_path / "a" / "b" / "trip.gpx")
    assert chemin.is_file()
