# -*- coding: utf-8 -*-
"""Tests du catalogue de stations et du calcul d'itinéraire."""

from __future__ import annotations

import json
import os

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
    os.utime(cache, (0, 0))  # rend le cache très périmé

    responses.add(responses.GET, routing.STATION_INFORMATION_URL, status=503)
    responses.add(responses.GET, routing.PARIS_OPENDATA_URL, status=503)

    catalog = StationCatalog.load(cache_path=cache)
    assert catalog.require("16107") is not None


@responses.activate
def test_cache_smovengo_perime_prefere_au_miroir_frais(station_payload, tmp_path) -> None:
    """Un cache Smovengo périmé DOIT primer sur un miroir frais et disponible.

    L'ordre est contre-intuitif — on préfère des données vieilles à des données
    fraîches — et c'est pourtant le bon : l'API privée Vélib' désigne ses
    stations par identifiant interne, que seul Smovengo publie. Le miroir
    n'expose que le code à cinq chiffres, donc un catalogue construit depuis lui
    ne résout AUCUN trajet réel, tout frais qu'il soit. Les coordonnées d'une
    station ne bougeant pratiquement jamais, la péremption ne coûte presque
    rien.

    Constaté en réel le 9 septembre 2026 : avec l'ordre inverse, une panne
    Smovengo rendait les 117 trajets à rattraper tous non géolocalisables.
    """
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps(station_payload), encoding="utf-8")
    os.utime(cache, (0, 0))  # très périmé

    responses.add(responses.GET, routing.STATION_INFORMATION_URL, status=503)
    responses.add(
        responses.GET, routing.PARIS_OPENDATA_URL, status=200,
        json={"total_count": 1, "results": [
            {"stationcode": "16107", "name": "Depuis le miroir",
             "coordonnees_geo": {"lon": 2.275725, "lat": 48.865983}},
        ]},
    )

    catalog = StationCatalog.load(cache_path=cache)

    # L'identifiant interne : le test décisif, le miroir ne le connaît pas.
    assert catalog.require("213688169").name == "Benjamin Godard - Victor Hugo"
    assert catalog.resolves_internal_ids
    # Périmé, donc non autoritaire : une station absente ne prouve pas sa
    # suppression, et main.py doit reporter le trajet plutôt que l'écarter.
    assert catalog.is_stale
    assert not catalog.is_authoritative
    # Le miroir n'a même pas été interrogé.
    assert not any(routing.PARIS_OPENDATA_URL in call.request.url
                   for call in responses.calls)


@responses.activate
def test_le_miroir_ne_doit_jamais_ecraser_un_cache_smovengo(
    station_payload, tmp_path
) -> None:
    """Le cache est le référentiel de secours du projet : ne pas le dégrader.

    Il est versionné dans le dépôt précisément parce que Smovengo est
    régulièrement injoignable, et le workflow le commite avec `if: always()`.
    Le remplacer par des données du miroir — qui n'expose pas les identifiants
    internes — détruirait donc définitivement la seule copie exploitable.

    Mesuré en réel le 9 septembre 2026 : 1471 stations avec identifiants
    internes remplacées par 1519 entrées sans, plus aucun trajet géolocalisable.
    """
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps(station_payload), encoding="utf-8")

    StationCatalog._write_cache(cache, {"paris_opendata_records": [{"stationcode": "16107"}]})

    conserve = json.loads(cache.read_text(encoding="utf-8"))
    assert "paris_opendata_records" not in conserve
    assert conserve["data"]["stations"][0]["station_id"] == 213688169


def test_un_cache_smovengo_est_bien_rafraichi(station_payload, tmp_path) -> None:
    """Le garde-fou ne doit pas bloquer une mise à jour de même rang."""
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps(station_payload), encoding="utf-8")

    frais = {"data": {"stations": [
        {"station_id": 999, "stationCode": "99999", "name": "Nouvelle",
         "lat": 48.86, "lon": 2.35},
    ]}}
    StationCatalog._write_cache(cache, frais)

    assert json.loads(cache.read_text(encoding="utf-8")) == frais


def test_un_cache_corrompu_n_empeche_pas_l_ecriture(tmp_path) -> None:
    """Rien à préserver dans un cache illisible : l'écriture doit passer."""
    cache = tmp_path / "cache.json"
    cache.write_text("{ ceci n'est pas du JSON", encoding="utf-8")

    charge = {"paris_opendata_records": [{"stationcode": "16107"}]}
    StationCatalog._write_cache(cache, charge)

    assert json.loads(cache.read_text(encoding="utf-8")) == charge


def test_le_miroir_peut_amorcer_un_cache_absent(tmp_path) -> None:
    """Sans cache préexistant, le miroir vaut mieux que rien : il s'écrit."""
    cache = tmp_path / "cache.json"
    charge = {"paris_opendata_records": [{"stationcode": "16107"}]}
    StationCatalog._write_cache(cache, charge)
    assert json.loads(cache.read_text(encoding="utf-8")) == charge


@responses.activate
def test_un_cache_miroir_perime_reste_un_dernier_recours(tmp_path) -> None:
    """Toutes les sources en panne et un cache issu du miroir : il sert encore.

    Il ne résoudra pas les identifiants internes, mais les trajets désignés par
    code à cinq chiffres passeront — mieux qu'un échec total.
    """
    cache = tmp_path / "cache.json"
    cache.write_text(
        json.dumps({"paris_opendata_records": [
            {"stationcode": "16107", "name": "Benjamin Godard",
             "coordonnees_geo": {"lon": 2.275725, "lat": 48.865983}},
        ]}),
        encoding="utf-8",
    )
    os.utime(cache, (0, 0))

    responses.add(responses.GET, routing.STATION_INFORMATION_URL, status=503)
    responses.add(responses.GET, routing.PARIS_OPENDATA_URL, status=503)

    catalog = StationCatalog.load(cache_path=cache)
    assert catalog.require("16107") is not None
    assert not catalog.resolves_internal_ids
    assert catalog.is_stale


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
