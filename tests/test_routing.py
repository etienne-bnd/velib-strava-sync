# -*- coding: utf-8 -*-
"""Tests du catalogue de stations et du calcul d'itinéraire."""

from __future__ import annotations

import json

import pytest
import responses

import routing
from routing import RoutingError, StationCatalog, StationNotFoundError


# --------------------------------------------------------------------------- #
# Géométrie
# --------------------------------------------------------------------------- #

def test_distance_haversine_connue() -> None:
    """Notre-Dame -> Tour Eiffel fait environ 4,2 km à vol d'oiseau."""
    distance = routing.haversine_distance((2.3522, 48.8566), (2.2945, 48.8584))
    assert 4100 < distance < 4350


def test_distance_nulle_pour_un_point_confondu() -> None:
    """Deux points identiques sont à distance nulle."""
    assert routing.haversine_distance((2.35, 48.85), (2.35, 48.85)) == 0.0


def test_longueur_de_polyligne() -> None:
    """La longueur d'une polyligne est la somme de ses segments."""
    points = [(2.30, 48.85), (2.31, 48.85), (2.32, 48.85)]
    total = routing.path_length(points)
    segment = routing.haversine_distance(points[0], points[1])
    assert total == pytest.approx(segment * 2, rel=1e-6)


# --------------------------------------------------------------------------- #
# Catalogue des stations
# --------------------------------------------------------------------------- #

def test_indexation_par_les_deux_identifiants(catalog) -> None:
    """Une station est trouvable par son station_id ET par son stationCode."""
    assert catalog.get("16107") is catalog.get("213688169")


def test_comptage_des_stations_distinctes(catalog) -> None:
    """Le double index ne doit pas gonfler artificiellement le décompte."""
    assert len(catalog) == 2


def test_zeros_non_significatifs_toleres(catalog) -> None:
    """Le code « 07001 » doit rester trouvable si l'API envoie « 7001 »."""
    assert catalog.get("07001") is not None


def test_zeros_ajoutes_si_la_source_les_omet() -> None:
    """Le miroir écrit « 7025 » là où Smovengo écrit « 07025 » : les deux
    graphies doivent mener à la même station, dans les deux sens."""
    catalogue = StationCatalog.from_paris_opendata(
        [{"stationcode": "7025", "name": "Octave Gréard",
          "coordonnees_geo": {"lon": 2.2925, "lat": 48.8570}}]
    )
    assert catalogue.get("7025") is not None
    assert catalogue.get("07025") is not None  # complété à cinq chiffres


def test_station_inconnue_retourne_none(catalog) -> None:
    """Un identifiant absent donne None plutôt qu'une exception."""
    assert catalog.get("99999") is None


def test_require_leve_pour_une_station_inconnue(catalog) -> None:
    """`require` transforme l'absence en erreur explicite."""
    with pytest.raises(StationNotFoundError, match="99999"):
        catalog.require("99999")


def test_stations_incompletes_ignorees() -> None:
    """Une station sans coordonnées est écartée sans faire échouer le reste."""
    payload = {
        "data": {
            "stations": [
                {"station_id": 1, "name": "Sans coordonnées"},
                {"station_id": 2, "stationCode": "2", "name": "Valide", "lat": 48.8, "lon": 2.3},
            ]
        }
    }
    assert len(StationCatalog.from_payload(payload)) == 1


def test_charge_utile_vide_leve_une_erreur() -> None:
    """Un open data vide doit être signalé, pas silencieusement accepté."""
    with pytest.raises(RoutingError, match="aucune station"):
        StationCatalog.from_payload({"data": {"stations": []}})


def test_miroir_paris_opendata() -> None:
    """Le miroir Paris Open Data produit un catalogue équivalent."""
    records = [
        {"stationcode": "16107", "name": "Benjamin Godard",
         "coordonnees_geo": {"lon": 2.275725, "lat": 48.865983}},
        {"stationcode": "07001", "name": "Sans coordonnées", "coordonnees_geo": {}},
    ]
    catalog = StationCatalog.from_paris_opendata(records)
    assert len(catalog) == 1
    assert catalog.require("16107").name == "Benjamin Godard"


@responses.activate
def test_repli_sur_le_miroir_quand_smovengo_echoue(station_payload, tmp_path) -> None:
    """Smovengo indisponible : le catalogue vient du miroir Paris Open Data."""
    responses.add(responses.GET, routing.STATION_INFORMATION_URL, status=503)
    responses.add(
        responses.GET, routing.PARIS_OPENDATA_URL, status=200,
        json={"total_count": 1, "results": [
            {"stationcode": "16107", "name": "Benjamin Godard",
             "coordonnees_geo": {"lon": 2.275725, "lat": 48.865983}},
        ]},
    )
    catalog = StationCatalog.load(cache_path=tmp_path / "cache.json")
    assert catalog.require("16107").name == "Benjamin Godard"


@responses.activate
def test_repli_sur_le_cache_perime(station_payload, tmp_path) -> None:
    """Les deux sources en panne : un cache périmé sauve l'exécution."""
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps(station_payload), encoding="utf-8")
    import os, time as _time
    os.utime(cache, (0, 0))  # rend le cache très périmé

    responses.add(responses.GET, routing.STATION_INFORMATION_URL, status=503)
    responses.add(responses.GET, routing.PARIS_OPENDATA_URL, status=503)

    catalog = StationCatalog.load(cache_path=cache)
    assert catalog.require("16107") is not None


@responses.activate
def test_echec_total_leve_une_erreur(tmp_path) -> None:
    """Sans source ni cache, l'erreur doit être explicite."""
    responses.add(responses.GET, routing.STATION_INFORMATION_URL, status=503)
    responses.add(responses.GET, routing.PARIS_OPENDATA_URL, status=503)
    with pytest.raises(RoutingError, match="Aucune source"):
        StationCatalog.load(cache_path=tmp_path / "absent.json")


@responses.activate
def test_cache_frais_evite_l_appel_reseau(station_payload, tmp_path) -> None:
    """Un cache récent doit être utilisé sans aucune requête HTTP."""
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps(station_payload), encoding="utf-8")
    catalog = StationCatalog.load(cache_path=cache)  # aucune réponse enregistrée
    assert len(catalog) == 2
    assert len(responses.calls) == 0


# --------------------------------------------------------------------------- #
# Calcul d'itinéraire
# --------------------------------------------------------------------------- #

ORS_GEOJSON_URL = f"{routing.ORS_DIRECTIONS_URL}/geojson"

ORS_REPONSE = {
    "features": [
        {
            "geometry": {
                "coordinates": [[2.2757, 48.8660], [2.2800, 48.8630], [2.2945, 48.8584]]
            },
            "properties": {"summary": {"distance": 1650.4, "duration": 400.0}},
        }
    ]
}


@responses.activate
def test_itineraire_nominal() -> None:
    """Une réponse valide est convertie en `Route` complète."""
    responses.add(responses.POST, ORS_GEOJSON_URL, json=ORS_REPONSE, status=200)
    route = routing.get_route_coordinates((2.2757, 48.8660), (2.2945, 48.8584), "cle")
    assert route.source == "openrouteservice"
    assert len(route) == 3
    assert route.distance_meters == pytest.approx(1650.4)
    assert route.points[0] == (2.2757, 48.8660)


@responses.activate
def test_cle_invalide_ne_declenche_pas_de_repli() -> None:
    """Une clé refusée est une erreur de configuration : inutile d'insister."""
    responses.add(responses.POST, ORS_GEOJSON_URL, status=403, json={})
    with pytest.raises(RoutingError, match="Clé OpenRouteService refusée"):
        routing.get_route_coordinates((2.27, 48.86), (2.29, 48.85), "mauvaise-cle")
    assert len(responses.calls) == 1  # aucun réessai


@responses.activate
def test_repli_ligne_droite_apres_echec(monkeypatch) -> None:
    """Un service indisponible donne un tracé approché, pas un échec."""
    monkeypatch.setattr(routing, "ORS_RETRY_BACKOFF", 0)
    responses.add(responses.POST, ORS_GEOJSON_URL, status=503, json={})
    route = routing.get_route_coordinates((2.2757, 48.8660), (2.2945, 48.8584), "cle")
    assert route.source == "fallback"
    assert route.points[0] == (2.2757, 48.8660)
    assert route.points[-1] == (2.2945, 48.8584)
    assert route.distance_meters > 0


@responses.activate
def test_repli_desactivable(monkeypatch) -> None:
    """`allow_fallback=False` propage l'erreur au lieu d'approximer."""
    monkeypatch.setattr(routing, "ORS_RETRY_BACKOFF", 0)
    responses.add(responses.POST, ORS_GEOJSON_URL, status=503, json={})
    with pytest.raises(RoutingError):
        routing.get_route_coordinates(
            (2.27, 48.86), (2.29, 48.85), "cle", allow_fallback=False
        )


@responses.activate
def test_quota_atteint_puis_succes(monkeypatch) -> None:
    """Un HTTP 429 est réessayé ; le succès suivant est bien pris en compte."""
    monkeypatch.setattr(routing, "ORS_RETRY_BACKOFF", 0)
    responses.add(responses.POST, ORS_GEOJSON_URL, status=429, json={})
    responses.add(responses.POST, ORS_GEOJSON_URL, json=ORS_REPONSE, status=200)
    route = routing.get_route_coordinates((2.27, 48.86), (2.29, 48.85), "cle")
    assert route.source == "openrouteservice"


@responses.activate
def test_geometrie_trop_courte_declenche_le_repli(monkeypatch) -> None:
    """Une réponse 200 mais inexploitable ne doit pas produire un GPX vide."""
    monkeypatch.setattr(routing, "ORS_RETRY_BACKOFF", 0)
    responses.add(
        responses.POST, ORS_GEOJSON_URL, status=200,
        json={"features": [{"geometry": {"coordinates": [[2.27, 48.86]]}, "properties": {}}]},
    )
    route = routing.get_route_coordinates((2.27, 48.86), (2.29, 48.85), "cle")
    assert route.source == "fallback"


def test_interpolation_en_ligne_droite() -> None:
    """L'interpolation respecte les extrémités et le nombre de points demandé."""
    points = routing._interpolate_straight_line((2.0, 48.0), (2.1, 48.1), 11)
    assert len(points) == 11
    assert points[0] == (2.0, 48.0)
    assert points[-1] == pytest.approx((2.1, 48.1))
    assert points[5] == pytest.approx((2.05, 48.05))


# --------------------------------------------------------------------------- #
# Complétude du catalogue selon sa source
# --------------------------------------------------------------------------- #

def test_smovengo_resout_les_identifiants_internes(station_payload) -> None:
    """L'open data Smovengo publie les identifiants internes : catalogue complet."""
    catalogue = StationCatalog.from_payload(station_payload)
    assert catalogue.source == StationCatalog.SOURCE_SMOVENGO
    assert catalogue.resolves_internal_ids is True
    assert catalogue.get("213688169") is not None


def test_miroir_ne_resout_pas_les_identifiants_internes() -> None:
    """Le miroir n'expose que les codes à cinq chiffres : catalogue dégradé.

    C'est l'information décisive : les trajets Vélib' désignent leurs stations
    par identifiant interne. Un catalogue dégradé ne peut en résoudre aucun, et
    l'orchestrateur doit le savoir pour reporter les trajets au lieu de les
    marquer traités.
    """
    catalogue = StationCatalog.from_paris_opendata(
        [{"stationcode": "16107", "name": "Benjamin Godard",
          "coordonnees_geo": {"lon": 2.275725, "lat": 48.865983}}]
    )
    assert catalogue.source == StationCatalog.SOURCE_PARIS_OPENDATA
    assert catalogue.resolves_internal_ids is False
    assert catalogue.get("213688169") is None
