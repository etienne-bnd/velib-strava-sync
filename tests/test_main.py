# -*- coding: utf-8 -*-
"""Tests de l'orchestrateur : sélection des trajets et parcours complet."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import responses

import config
import gpx_builder
import main
import routing
import strava
import velib
from main import TripSkipped, build_activity_name, process_trip, select_trips
from models import Route, VelibTrip
from state import ProcessedTripsState

MAINTENANT = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _sans_pause(monkeypatch) -> None:
    """Supprime les temporisations de courtoisie pendant les tests."""
    monkeypatch.setattr(main, "ORS_DELAY_SECONDS", 0)
    monkeypatch.setattr(main, "STRAVA_DELAY_SECONDS", 0)
    monkeypatch.setattr(strava, "UPLOAD_POLL_INTERVAL", 0)


@pytest.fixture
def settings(tmp_path) -> config.Config:
    """Configuration de test, isolée dans un répertoire temporaire."""
    return config.Config(
        velib_username="a@b.fr", velib_password="motdepasse",
        strava_client_id="123", strava_client_secret="secret",
        strava_refresh_token="rafraichissement", ors_api_key="cle-ors",
        repo_url="https://github.com/u/velibsurstrava",
        state_file=tmp_path / "processed_trips.json",
        gpx_file=tmp_path / "temp_trip.gpx",
        max_trips_per_run=25, max_trip_age_days=30, dry_run=False,
    )


def _trip(trip_id: str, jours: int = 1, duree: int = 900,
          depart: str = "16107", arrivee: str = "07001") -> VelibTrip:
    return VelibTrip(
        trip_id, MAINTENANT - timedelta(days=jours), duree, depart, arrivee
    )


# --------------------------------------------------------------------------- #
# Sélection des trajets
# --------------------------------------------------------------------------- #

def test_trajet_deja_traite_ecarte(settings) -> None:
    """Un trajet présent dans l'état n'est pas re-sélectionné."""
    etat = ProcessedTripsState.load(settings.state_file)
    deja = _trip("t1", jours=1)
    # Heure de départ distincte : la clé temporelle de repli ne doit capter que
    # le trajet réellement traité.
    nouveau = _trip("t2", jours=2)
    etat.mark_processed(deja)
    assert [t.trip_id for t in select_trips([deja, nouveau], etat, settings)] == ["t2"]


def test_trajet_trop_ancien_ecarte(settings) -> None:
    """Au-delà de MAX_TRIP_AGE_DAYS, le trajet est ignoré."""
    etat = ProcessedTripsState.load(settings.state_file)
    retenus = select_trips([_trip("vieux", jours=60), _trip("recent", jours=2)], etat, settings)
    assert [t.trip_id for t in retenus] == ["recent"]


def test_age_illimite_si_zero(settings) -> None:
    """MAX_TRIP_AGE_DAYS=0 désactive le filtre d'ancienneté."""
    from dataclasses import replace

    etat = ProcessedTripsState.load(settings.state_file)
    reglages = replace(settings, max_trip_age_days=0)
    assert len(select_trips([_trip("vieux", jours=400)], etat, reglages)) == 1


def test_annulation_ecartee(settings) -> None:
    """Une boucle de moins de deux minutes est une annulation : ignorée."""
    etat = ProcessedTripsState.load(settings.state_file)
    annulation = _trip("annule", duree=90, depart="16107", arrivee="16107")
    assert select_trips([annulation], etat, settings) == []


def test_boucle_longue_conservee(settings) -> None:
    """Une boucle de 40 min est un vrai trajet : elle passe la sélection."""
    etat = ProcessedTripsState.load(settings.state_file)
    boucle = _trip("boucle", duree=2400, depart="16107", arrivee="16107")
    assert len(select_trips([boucle], etat, settings)) == 1


def test_plafond_par_execution(settings) -> None:
    """Le plafond protège le quota ORS ; le reste attend le lendemain."""
    from dataclasses import replace

    etat = ProcessedTripsState.load(settings.state_file)
    reglages = replace(settings, max_trips_per_run=2)
    trajets = [_trip(f"t{i}", jours=i + 1) for i in range(5)]
    assert len(select_trips(trajets, etat, reglages)) == 2


# --------------------------------------------------------------------------- #
# Nom de l'activité
# --------------------------------------------------------------------------- #

def test_nom_d_activite_avec_les_deux_stations(catalog) -> None:
    """Le nom mentionne les deux stations et le moment de la journée."""
    trajet = VelibTrip("t", datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc), 900,
                       "16107", "07001")
    nom = build_activity_name(trajet, catalog.require("16107"), catalog.require("07001"))
    assert "matinal" in nom and "Benjamin Godard" in nom and "Tour Eiffel" in nom


def test_nom_d_activite_pour_une_boucle(catalog) -> None:
    """Une boucle est nommée comme telle, sans flèche."""
    trajet = VelibTrip("t", datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc), 2400,
                       "16107", "16107")
    nom = build_activity_name(trajet, catalog.require("16107"), catalog.require("16107"))
    assert "boucle" in nom and "→" not in nom


# --------------------------------------------------------------------------- #
# Traitement d'un trajet
# --------------------------------------------------------------------------- #

def test_station_inconnue_ecarte_le_trajet(catalog, settings) -> None:
    """Une station absente de l'open data rend le trajet inexploitable."""
    with pytest.raises(TripSkipped, match="99999"):
        process_trip(_trip("t", depart="99999"), catalog, "jeton", settings)


def test_boucle_longue_ecartee_au_traitement(catalog, settings) -> None:
    """Une boucle n'a pas d'itinéraire calculable : elle est écartée, pas plantée."""
    boucle = _trip("boucle", duree=2400, depart="16107", arrivee="16107")
    with pytest.raises(TripSkipped, match="Boucle"):
        process_trip(boucle, catalog, "jeton", settings)


@responses.activate
def test_traitement_complet_d_un_trajet(catalog, settings) -> None:
    """Parcours nominal : itinéraire, GPX écrit, envoi et identifiant retourné."""
    responses.add(
        responses.POST, f"{routing.ORS_DIRECTIONS_URL}/geojson", status=200,
        json={"features": [{
            "geometry": {"coordinates": [[2.2757, 48.8660], [2.28, 48.863], [2.2945, 48.8584]]},
            "properties": {"summary": {"distance": 1650.4}},
        }]},
    )
    responses.add(responses.POST, strava.UPLOADS_URL, json={"id": 555}, status=201)
    responses.add(
        responses.GET, f"{strava.UPLOADS_URL}/555", json={"activity_id": 987654}, status=200
    )

    activite, source = process_trip(_trip("t"), catalog, "jeton", settings)
    assert activite == "987654"
    assert source == "openrouteservice"
    assert Path(settings.gpx_file).is_file()

    # La description envoyée doit citer le dépôt et la distance.
    corps = responses.calls[1].request.body.decode("utf-8", errors="replace")
    assert "https://github.com/u/velibsurstrava" in corps
    assert "1.65 km" in corps


@responses.activate
def test_mode_simulation_n_envoie_rien(catalog, settings) -> None:
    """En simulation, le GPX est produit mais aucun appel Strava n'est émis."""
    from dataclasses import replace

    responses.add(
        responses.POST, f"{routing.ORS_DIRECTIONS_URL}/geojson", status=200,
        json={"features": [{
            "geometry": {"coordinates": [[2.2757, 48.8660], [2.2945, 48.8584]]},
            "properties": {"summary": {"distance": 1650.4}},
        }]},
    )
    activite, note = process_trip(_trip("t"), catalog, "", replace(settings, dry_run=True))
    assert activite is None and "simulation" in note
    assert Path(settings.gpx_file).is_file()
    assert len(responses.calls) == 1  # uniquement l'appel ORS


# --------------------------------------------------------------------------- #
# Boucle d'exécution
# --------------------------------------------------------------------------- #

def _installer_faux_modules(monkeypatch, catalog, trajets, resultat_ou_erreur) -> list:
    """Remplace les dépendances externes de `run` par des doublures."""
    monkeypatch.setattr(velib, "get_new_velib_trips", lambda u, p: trajets)
    monkeypatch.setattr(main.velib, "get_new_velib_trips", lambda u, p: trajets)
    monkeypatch.setattr(
        main.routing.StationCatalog, "load", classmethod(lambda cls, **k: catalog)
    )
    monkeypatch.setattr(main.strava, "get_access_token", lambda *a: "jeton")

    appels: list[VelibTrip] = []

    def _process(trip, cat, token, cfg):
        appels.append(trip)
        if isinstance(resultat_ou_erreur, Exception):
            raise resultat_ou_erreur
        return resultat_ou_erreur

    monkeypatch.setattr(main, "process_trip", _process)
    return appels


def test_run_sans_nouveau_trajet(monkeypatch, catalog, settings) -> None:
    """Aucun trajet à traiter : sortie propre, aucun fichier d'état écrit."""
    _installer_faux_modules(monkeypatch, catalog, [], ("1", "openrouteservice"))
    assert main.run(settings) == main.EXIT_OK
    assert not settings.state_file.exists()


def test_run_marque_les_trajets_envoyes(monkeypatch, catalog, settings) -> None:
    """Chaque envoi réussi est enregistré dans l'état, sur disque."""
    trajets = [_trip("t1"), _trip("t2", jours=2)]
    _installer_faux_modules(monkeypatch, catalog, trajets, ("987", "openrouteservice"))

    assert main.run(settings) == main.EXIT_OK
    etat = ProcessedTripsState.load(settings.state_file)
    assert etat.contains(trajets[0]) and etat.contains(trajets[1])


def test_echec_transitoire_ne_marque_pas_le_trajet(monkeypatch, catalog, settings) -> None:
    """Un échec réseau doit laisser le trajet reprogrammable pour le lendemain."""
    trajet = _trip("t1")
    _installer_faux_modules(
        monkeypatch, catalog, [trajet], strava.StravaError("panne temporaire")
    )
    main.run(settings)
    assert ProcessedTripsState.load(settings.state_file).contains(trajet) is False


def test_trajet_inexploitable_est_marque(monkeypatch, catalog, settings) -> None:
    """Un échec définitif est marqué, pour ne pas être réexaminé chaque jour."""
    trajet = _trip("t1")
    _installer_faux_modules(monkeypatch, catalog, [trajet], TripSkipped("station inconnue"))
    assert main.run(settings) == main.EXIT_OK
    assert ProcessedTripsState.load(settings.state_file).contains(trajet) is True


def test_doublon_strava_est_marque(monkeypatch, catalog, settings) -> None:
    """Un doublon signifie que l'objectif est atteint : le trajet est marqué."""
    trajet = _trip("t1")
    _installer_faux_modules(
        monkeypatch, catalog, [trajet], strava.StravaDuplicateError("déjà présent")
    )
    assert main.run(settings) == main.EXIT_OK
    assert ProcessedTripsState.load(settings.state_file).contains(trajet) is True


def test_quota_strava_interrompt_l_execution(monkeypatch, catalog, settings) -> None:
    """Le quota épuisé arrête la boucle plutôt que d'enchaîner les échecs."""
    trajets = [_trip(f"t{i}", jours=i + 1) for i in range(5)]
    appels = _installer_faux_modules(
        monkeypatch, catalog, trajets, strava.StravaRateLimitError("quota")
    )
    main.run(settings)
    assert len(appels) == 1  # arrêt dès le premier refus


def test_blocage_cloudflare_code_de_sortie(monkeypatch, catalog, settings) -> None:
    """Un blocage anti-bot a son propre code de sortie, distinguable en CI."""
    def _bloque(u, p):
        raise velib.CloudflareChallenge("challenge")

    monkeypatch.setattr(main.velib, "get_new_velib_trips", _bloque)
    assert main.run(settings) == main.EXIT_ANTIBOT


def test_main_sans_configuration(monkeypatch) -> None:
    """Une configuration incomplète donne le code de sortie 2."""
    def _echoue():
        raise config.ConfigError("variables manquantes")

    monkeypatch.setattr(main.config, "load_config", _echoue)
    assert main.main([]) == main.EXIT_CONFIG


# --------------------------------------------------------------------------- #
# Catalogue dégradé : les trajets doivent être reportés, jamais perdus
# --------------------------------------------------------------------------- #

@pytest.fixture
def catalogue_degrade():
    """Catalogue issu du miroir : ne résout pas les identifiants internes."""
    return routing.StationCatalog.from_paris_opendata(
        [{"stationcode": "16107", "name": "Benjamin Godard",
          "coordonnees_geo": {"lon": 2.275725, "lat": 48.865983}}]
    )


def test_catalogue_degrade_leve_une_erreur_distincte(catalogue_degrade, settings) -> None:
    """Un identifiant interne non résolu par le miroir n'est pas un défaut du trajet."""
    trajet = _trip("t", depart="128920403", arrivee="52456")
    with pytest.raises(main.CatalogDegraded):
        process_trip(trajet, catalogue_degrade, "jeton", settings)


def test_catalogue_degrade_ne_marque_aucun_trajet(
    monkeypatch, catalogue_degrade, settings
) -> None:
    """C'est le scénario critique : Smovengo injoignable un jour donné.

    Sans ce garde-fou, tous les trajets seraient marqués « station inconnue »
    et perdus définitivement, alors qu'ils redeviendront synchronisables dès le
    retour de Smovengo.
    """
    trajets = [_trip(f"t{i}", jours=i + 1) for i in range(4)]
    _installer_faux_modules(
        monkeypatch, catalogue_degrade, trajets,
        main.CatalogDegraded("miroir sans identifiants internes"),
    )
    main.run(settings)

    etat = ProcessedTripsState.load(settings.state_file)
    assert all(not etat.contains(t) for t in trajets)


def test_catalogue_degrade_arrete_la_boucle(
    monkeypatch, catalogue_degrade, settings
) -> None:
    """Inutile d'insister : tous les trajets échoueraient pour la même raison."""
    trajets = [_trip(f"t{i}", jours=i + 1) for i in range(5)]
    appels = _installer_faux_modules(
        monkeypatch, catalogue_degrade, trajets,
        main.CatalogDegraded("miroir sans identifiants internes"),
    )
    main.run(settings)
    assert len(appels) == 1


# --------------------------------------------------------------------------- #
# Contrôle de cohérence des distances
# --------------------------------------------------------------------------- #

def _trajet_avec_distance(distance_m: float) -> VelibTrip:
    trajet = _trip("t")
    trajet.distance_meters = distance_m
    return trajet


@pytest.mark.parametrize("distance_ors", [3100.0, 2000.0, 5500.0])
def test_distance_coherente_ne_signale_rien(distance_ors, caplog, catalog) -> None:
    """Un écart d'itinéraire ordinaire ne doit pas produire d'avertissement."""
    route = Route(points=[(2.27, 48.86), (2.29, 48.85)], distance_meters=distance_ors)
    with caplog.at_level("WARNING"):
        main._verifier_coherence_distance(
            _trajet_avec_distance(3084.0), route,
            catalog.require("16107"), catalog.require("07001"),
        )
    assert caplog.text == ""


@pytest.mark.parametrize("distance_ors", [800.0, 12000.0])
def test_distance_aberrante_signalee(distance_ors, caplog, catalog) -> None:
    """Un rapport hors de [0,5 ; 2] trahit une station mal identifiée."""
    route = Route(points=[(2.27, 48.86), (2.29, 48.85)], distance_meters=distance_ors)
    with caplog.at_level("WARNING"):
        main._verifier_coherence_distance(
            _trajet_avec_distance(3084.0), route,
            catalog.require("16107"), catalog.require("07001"),
        )
    assert "Vérifier la correspondance des stations" in caplog.text


def test_distance_absente_ne_declenche_rien(caplog, catalog) -> None:
    """Sans distance Vélib', il n'y a rien à comparer."""
    route = Route(points=[(2.27, 48.86)], distance_meters=3000.0)
    with caplog.at_level("WARNING"):
        main._verifier_coherence_distance(
            _trip("t"), route, catalog.require("16107"), catalog.require("07001")
        )
    assert caplog.text == ""
