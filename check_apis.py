#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnostic : vérifie que chaque API répond avec les identifiants configurés.

Ce script n'écrit rien et n'envoie aucune activité. Il valide, une par une, les
quatre dépendances externes du projet :

  1. l'open data des stations (Smovengo, puis son miroir Paris Open Data) ;
  2. l'API OpenRouteService, sur un itinéraire réel dans Paris ;
  3. l'API Strava, jusqu'à l'identité du compte, sans rien y écrire ;
  4. l'API privée Vélib', jusqu'à la lecture de l'historique.

Usage :
    python check_apis.py
    python check_apis.py --skip-velib   # ignore la connexion Vélib'
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

import requests

import config
import gpx_builder
import main as orchestrateur
import routing
import strava
import velib

logger = logging.getLogger("diagnostic")

# Deux stations réelles et distantes d'environ 1,7 km, pour éprouver le routage.
STATION_DEPART = "16107"   # Benjamin Godard - Victor Hugo
STATION_ARRIVEE = "7025"   # Octave Gréard - Tour Eiffel
STATION_ID_INTERNE = "52456"  # Pré Saint-Gervais - Lilas, identifiant interne

OK = "\033[32m✓\033[0m"
KO = "\033[31m✗\033[0m"
WARN = "\033[33m!\033[0m"


def titre(texte: str) -> None:
    """Affiche un séparateur de section."""
    print(f"\n\033[1m{texte}\033[0m")
    print("─" * 62)


def verifier_stations() -> routing.StationCatalog | None:
    """Vérifie l'accès à l'open data des stations.

    Returns:
        Le catalogue si au moins une source répond, sinon None.
    """
    titre("1. Open data des stations Vélib'")

    for nom, url in (
        ("Smovengo", routing.STATION_INFORMATION_URL),
        ("Paris Open Data", routing.PARIS_OPENDATA_URL),
    ):
        try:
            reponse = requests.get(url, timeout=20, params={"limit": 1})
            print(f"  {OK if reponse.ok else KO} {nom} : HTTP {reponse.status_code}")
        except requests.RequestException as exc:
            print(f"  {KO} {nom} : {type(exc).__name__}")

    # Même chemin de cache que la production, pour tester ce qui tourne vraiment.
    try:
        catalogue = routing.StationCatalog.load(cache_path=orchestrateur.STATIONS_CACHE)
    except routing.RoutingError as exc:
        print(f"  {KO} Aucune source accessible : {exc}")
        return None

    print(f"  {OK} Catalogue chargé : {len(catalogue)} stations "
          f"(source : {catalogue.source}, périmé : {catalogue.is_stale})")

    # Contrôle décisif : l'API Vélib' désigne ses stations par identifiant
    # interne, que seul l'open data Smovengo publie.
    if catalogue.get(STATION_ID_INTERNE):
        print(f"  {OK} Identifiants internes résolus (test sur {STATION_ID_INTERNE})")
    else:
        print(f"  {KO} Identifiants internes NON résolus : aucun trajet réel "
              "ne pourra être géolocalisé.")
    for code in (STATION_DEPART, STATION_ARRIVEE):
        station = catalogue.get(code)
        if station:
            print(f"  {OK} Station {code} : {station.name} {station.coordinates}")
        else:
            print(f"  {WARN} Station {code} introuvable")
    return catalogue


def verifier_openrouteservice(
    reglages: config.Config, catalogue: routing.StationCatalog | None
) -> bool:
    """Calcule un itinéraire réel et construit le GPX correspondant.

    Args:
        reglages: Configuration chargée.
        catalogue: Catalogue des stations, ou None s'il est indisponible.

    Returns:
        True si l'itinéraire a été calculé par OpenRouteService.
    """
    titre("2. API OpenRouteService")

    if catalogue is None:
        depart, arrivee = (2.275725, 48.865983), (2.294480, 48.858370)
        print(f"  {WARN} Catalogue indisponible : coordonnées fixes utilisées")
    else:
        station_a, station_b = catalogue.get(STATION_DEPART), catalogue.get(STATION_ARRIVEE)
        if not station_a or not station_b:
            print(f"  {KO} Stations de test introuvables")
            return False
        depart, arrivee = station_a.coordinates, station_b.coordinates

    try:
        route = routing.get_route_coordinates(
            depart, arrivee, reglages.ors_api_key, allow_fallback=False
        )
    except routing.RoutingError as exc:
        print(f"  {KO} {exc}")
        return False

    print(f"  {OK} Itinéraire calculé : {len(route)} points")
    print(f"  {OK} Distance : {route.distance_meters / 1000:.2f} km")

    # Enchaîne sur la construction du GPX : c'est le maillon suivant.
    try:
        chemin = gpx_builder.build_gpx(
            route.points,
            datetime.now(timezone.utc),
            900,
            "Diagnostic velibsurstrava",
            "/tmp/diagnostic_velib.gpx",
        )
    except gpx_builder.GpxBuildError as exc:
        print(f"  {KO} Construction du GPX : {exc}")
        return False

    taille = chemin.stat().st_size
    vitesse = route.distance_meters / 900 * 3.6
    print(f"  {OK} GPX construit : {chemin} ({taille} octets, {vitesse:.1f} km/h simulés)")
    return True


def verifier_strava(reglages: config.Config) -> bool:
    """Rafraîchit le jeton Strava et lit l'identité du compte.

    Aucune activité n'est créée : seul un appel en lecture est effectué.

    Args:
        reglages: Configuration chargée.

    Returns:
        True si le jeton est valide et porte le scope d'écriture.
    """
    titre("3. API Strava")

    try:
        jeton = strava.get_access_token(
            reglages.strava_client_id,
            reglages.strava_client_secret,
            reglages.strava_refresh_token,
        )
    except strava.StravaScopeError as exc:
        print(f"  {KO} Scope insuffisant.\n      " + str(exc).replace("\n", "\n      "))
        return False
    except strava.StravaError as exc:
        print(f"  {KO} {exc}")
        return False

    print(f"  {OK} Jeton d'accès obtenu ({jeton[:6]}…)")

    try:
        reponse = requests.get(
            f"{strava.API_BASE}/athlete",
            headers={"Authorization": f"Bearer {jeton}"},
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"  {KO} Lecture du profil impossible : {exc}")
        return False

    if not reponse.ok:
        print(f"  {KO} /athlete : HTTP {reponse.status_code} — {reponse.text[:200]}")
        return False

    athlete = reponse.json()
    prenom = athlete.get("firstname", "")
    nom = athlete.get("lastname", "")
    print(f"  {OK} Compte : {prenom} {nom} (id {athlete.get('id')})")

    # Si l'on est arrivé ici, get_access_token a validé la présence du scope
    # activity:write : il n'y a plus d'incertitude à signaler.
    print(f"  {OK} Scope « {strava.REQUIRED_SCOPE} » accordé")

    quota = reponse.headers.get("X-RateLimit-Usage")
    if quota:
        print(f"  {OK} Quota utilisé : {quota} (limite {reponse.headers.get('X-RateLimit-Limit')})")
    return True


def verifier_velib(reglages: config.Config) -> bool:
    """Se connecte au compte Vélib' et lit l'historique des trajets.

    Args:
        reglages: Configuration chargée.

    Returns:
        True si l'historique a pu être lu.
    """
    titre("4. API privée Vélib' Métropole")

    try:
        trajets = velib.get_new_velib_trips(
            reglages.velib_username, reglages.velib_password
        )
    except velib.CloudflareChallenge as exc:
        print(f"  {KO} Blocage anti-bot : {exc}")
        print("      Attendu depuis une IP de centre de données ; à retester")
        print("      depuis une connexion résidentielle ou un runner auto-hébergé.")
        return False
    except velib.VelibError as exc:
        print(f"  {KO} {exc}")
        return False

    print(f"  {OK} Connexion réussie, {len(trajets)} trajets exploitables")
    if not trajets:
        print(f"  {WARN} Historique vide : aucun trajet à synchroniser.")
        return True

    print("\n  Cinq trajets les plus récents :")
    for trajet in trajets[-5:]:
        print(
            f"    · {trajet.start_time.strftime('%d/%m/%Y %H:%M UTC')} — "
            f"{trajet.duration_seconds // 60:>3} min — "
            f"{trajet.departure_station_id} → {trajet.arrival_station_id}"
        )

    # Les clés brutes aident à ajuster les listes de champs candidats si le
    # schéma de l'API a changé.
    print(f"\n  Champs du dernier enregistrement brut :")
    print(f"    {sorted(trajets[-1].raw.keys())}")
    return True


def main(argv: list[str] | None = None) -> int:
    """Point d'entrée du diagnostic.

    Args:
        argv: Arguments de ligne de commande.

    Returns:
        0 si tout répond, 1 sinon.
    """
    parser = argparse.ArgumentParser(description="Vérifie l'accès aux API du projet.")
    parser.add_argument("--skip-velib", action="store_true",
                        help="Ne teste pas la connexion au compte Vélib'.")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Journalisation détaillée.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        reglages = config.load_config()
    except config.ConfigError as exc:
        print(f"{KO} Configuration : {exc}")
        return 1

    print(f"{OK} Configuration chargée (compte Vélib' : {reglages.velib_username})")

    catalogue = verifier_stations()
    resultats = {
        "Stations": catalogue is not None,
        "OpenRouteService": verifier_openrouteservice(reglages, catalogue),
        "Strava": verifier_strava(reglages),
    }
    if not args.skip_velib:
        resultats["Vélib'"] = verifier_velib(reglages)

    titre("Bilan")
    for nom, etat in resultats.items():
        print(f"  {OK if etat else KO} {nom}")

    return 0 if all(resultats.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
