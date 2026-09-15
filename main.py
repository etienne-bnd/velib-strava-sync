#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Orchestrateur : synchronise les trajets Vélib' vers Strava.

Enchaînement d'une exécution :

  1. charger la configuration et l'état des trajets déjà traités ;
  2. récupérer l'historique Vélib' ;
  3. écarter les trajets déjà envoyés, trop anciens, ou annulés ;
  4. pour chaque trajet retenu : géolocaliser les stations, calculer
     l'itinéraire cyclable, produire le GPX horodaté, l'envoyer sur Strava ;
  5. enregistrer l'état après chaque succès.

L'état est écrit après chaque trajet et non à la fin : si l'exécution est
interrompue — quota épuisé, coupure du runner —, les trajets déjà envoyés ne
seront pas renvoyés le lendemain.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import config
import gpx_builder
import routing
import strava
import velib
from models import Route, Station, VelibTrip
from state import ProcessedTripsState

logger = logging.getLogger("velibsurstrava")

# Pauses de courtoisie vis-à-vis des quotas gratuits. OpenRouteService autorise
# 40 requêtes/minute ; Strava, 200 requêtes/quart d'heure.
ORS_DELAY_SECONDS = 2.0
STRAVA_DELAY_SECONDS = 3.0

# Vélib' publie la distance réelle du trajet (`parameter3.DISTANCE`). La
# comparer à celle calculée par OpenRouteService détecte gratuitement une
# correspondance de station erronée : un écart d'un facteur deux ne s'explique
# pas par un choix d'itinéraire.
DISTANCE_RATIO_MIN = 0.5
DISTANCE_RATIO_MAX = 2.0

STATIONS_CACHE = config.PROJECT_ROOT / "stations_cache.json"

# Codes de sortie, pour que GitHub Actions distingue les causes d'échec.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2
EXIT_ANTIBOT = 3


class TripSkipped(Exception):
    """Le trajet est inexploitable de façon définitive ; il sera marqué traité."""


class CatalogDegraded(Exception):
    """Le catalogue ne sait résoudre AUCUN identifiant interne de station.

    C'est une panne systémique et temporaire, pas un défaut du trajet : le
    trajet ne doit surtout PAS être marqué comme traité, sans quoi il serait
    perdu définitivement dès la première exécution où Smovengo est injoignable.

    Cette exception interrompt l'exécution entière, ce qui n'est justifié que
    parce qu'aucun trajet suivant ne pourrait aboutir : elle est réservée au
    catalogue issu du miroir Paris Open Data, qui n'expose que les codes à cinq
    chiffres là où l'API Vélib' désigne ses stations par identifiant interne.
    """


class TripDeferred(Exception):
    """Cette station-là est absente d'un catalogue par ailleurs exploitable.

    Distinguer ce cas de `CatalogDegraded` est essentiel. Un cache Smovengo
    périmé résout la quasi-totalité des trajets ; seules lui manquent les
    stations créées après sa constitution. Traiter cette lacune ponctuelle
    comme une panne générale a coûté 92 trajets sur 119 lors du rattrapage du
    15 septembre 2026 : l'exécution s'est arrêtée au 27ᵉ trajet parce qu'une
    seule station était inconnue.

    Le trajet n'est pas marqué traité : il repartira à la prochaine exécution,
    et aboutira dès que Smovengo sera de nouveau joignable.
    """


def configure_logging(verbose: bool = False) -> None:
    """Configure la journalisation sur la sortie d'erreur.

    Args:
        verbose: Active le niveau DEBUG.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # `urllib3` est très bavard en DEBUG et noierait les messages utiles.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def select_trips(
    trips: list[VelibTrip], state: ProcessedTripsState, settings: config.Config
) -> list[VelibTrip]:
    """Filtre les trajets à traiter lors de cette exécution.

    Args:
        trips: Trajets normalisés issus de l'API Vélib'.
        state: État des trajets déjà synchronisés.
        settings: Configuration de l'exécution.

    Returns:
        Les trajets retenus, du plus ancien au plus récent, dans la limite de
        `max_trips_per_run`.
    """
    cutoff: datetime | None = None
    if settings.max_trip_age_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.max_trip_age_days)

    selected: list[VelibTrip] = []
    counters = {"déjà traités": 0, "trop anciens": 0, "annulations": 0}

    for trip in trips:
        if state.contains(trip):
            counters["déjà traités"] += 1
            continue
        if cutoff is not None and trip.start_time < cutoff:
            counters["trop anciens"] += 1
            continue
        if trip.is_probable_cancellation:
            # Boucle de moins de deux minutes : location annulée, aucun
            # déplacement à reconstituer.
            counters["annulations"] += 1
            logger.info(
                "Trajet %s ignoré : boucle de %d s à la station %s (annulation).",
                trip.trip_id, trip.duration_seconds, trip.departure_station_id,
            )
            continue
        selected.append(trip)

    logger.info(
        "%d trajets récupérés — écartés : %s.",
        len(trips),
        ", ".join(f"{count} {label}" for label, count in counters.items()),
    )

    if len(selected) > settings.max_trips_per_run:
        logger.warning(
            "%d trajets éligibles, plafonnés à %d pour cette exécution ; "
            "le reste sera traité demain.",
            len(selected), settings.max_trips_per_run,
        )
        selected = selected[: settings.max_trips_per_run]

    return selected


def resolve_stations(
    trip: VelibTrip, catalog: routing.StationCatalog
) -> tuple[Station, Station]:
    """Retrouve les stations de départ et d'arrivée dans l'open data.

    Args:
        trip: Trajet à géolocaliser.
        catalog: Catalogue des stations.

    Returns:
        Le couple (station de départ, station d'arrivée).

    Raises:
        CatalogDegraded: Si le catalogue ne résout aucun identifiant interne.
        TripDeferred: Si le catalogue est simplement périmé et ignore CETTE
            station.
        TripSkipped: Si l'une des deux stations est introuvable dans un
            catalogue faisant autorité — la station a donc réellement disparu.
    """
    try:
        departure = catalog.require(trip.departure_station_id)
        arrival = catalog.require(trip.arrival_station_id)
    except routing.StationNotFoundError as exc:
        stations = f"{trip.departure_station_id}/{trip.arrival_station_id}"

        if not catalog.resolves_internal_ids:
            # Le miroir n'expose que les codes à cinq chiffres : aucun trajet
            # désigné par identifiant interne ne pourra aboutir. Inutile de
            # dérouler les suivants, ils échoueront tous de la même façon.
            raise CatalogDegraded(
                f"Station {stations} non résoluble : le catalogue (source : "
                f"{catalog.source}) ne publie pas les identifiants internes."
            ) from exc

        if catalog.is_stale:
            # Le catalogue résout bien les identifiants internes, il date
            # simplement. Cette station-ci a pu être créée depuis : c'est une
            # lacune ponctuelle, pas une panne. Reporter CE trajet et continuer.
            raise TripDeferred(
                f"Station {stations} absente du cache Smovengo périmé "
                "(vraisemblablement créée depuis). Trajet reporté ; les "
                "suivants sont tentés normalement."
            ) from exc

        raise TripSkipped(str(exc)) from exc
    return departure, arrival


def _verifier_coherence_distance(
    trip: VelibTrip, route: Route, departure: Station, arrival: Station
) -> None:
    """Signale un écart marqué entre la distance Vélib' et celle calculée.

    Un itinéraire cyclable est rarement plus court que le trajet réel ni deux
    fois plus long : hors de cette fourchette, l'explication la plus probable
    est une station mal identifiée, donc un tracé au mauvais endroit.

    Args:
        trip: Trajet concerné.
        route: Itinéraire calculé.
        departure: Station de départ retenue.
        arrival: Station d'arrivée retenue.
    """
    if not trip.distance_meters or not route.distance_meters:
        return
    if trip.distance_meters <= 0:
        return

    ratio = route.distance_meters / trip.distance_meters
    if DISTANCE_RATIO_MIN <= ratio <= DISTANCE_RATIO_MAX:
        return

    logger.warning(
        "Trajet %s : distance calculée %.0f m contre %.0f m annoncés par Vélib' "
        "(rapport %.2f). Vérifier la correspondance des stations %s (%s) et "
        "%s (%s).",
        trip.trip_id, route.distance_meters, trip.distance_meters, ratio,
        trip.departure_station_id, departure.name,
        trip.arrival_station_id, arrival.name,
    )


def _verifier_activite_creee(
    access_token: str, activity_id: int | str, trip: VelibTrip, route: Route
) -> None:
    """Relit l'activité créée et journalise ce que Strava en a retenu.

    Silencieux si le jeton ne porte pas de scope de lecture : la vérification
    est un confort, pas une condition de succès.

    Args:
        access_token: Jeton d'accès valide.
        activity_id: Identifiant de l'activité créée.
        trip: Trajet à l'origine de l'activité.
        route: Itinéraire envoyé.
    """
    activite = strava.fetch_activity(access_token, activity_id)
    if activite is None:
        logger.info(
            "Activité %s créée (relecture impossible : jeton sans scope de "
            "lecture). https://www.strava.com/activities/%s",
            activity_id, activity_id,
        )
        return

    distance = activite.get("distance") or 0
    duree = activite.get("elapsed_time") or 0
    logger.info(
        "Activité %s vérifiée : « %s », %.2f km, %d s, type %s. "
        "https://www.strava.com/activities/%s",
        activity_id, activite.get("name"), distance / 1000, duree,
        activite.get("sport_type") or activite.get("type"), activity_id,
    )

    # Strava recalcule distance et durée à partir du GPX : un écart marqué
    # signalerait un fichier mal interprété.
    if route.distance_meters and distance:
        ecart = abs(distance - route.distance_meters) / route.distance_meters
        if ecart > 0.1:
            logger.warning(
                "Activité %s : Strava a retenu %.0f m contre %.0f m envoyés "
                "(écart de %.0f %%).",
                activity_id, distance, route.distance_meters, ecart * 100,
            )
    if duree and abs(duree - trip.duration_seconds) > 60:
        logger.warning(
            "Activité %s : Strava a retenu %d s contre %d s envoyées.",
            activity_id, duree, trip.duration_seconds,
        )


def build_activity_name(trip: VelibTrip, departure: Station, arrival: Station) -> str:
    """Compose le nom de l'activité Strava.

    Args:
        trip: Trajet concerné.
        departure: Station de départ.
        arrival: Station d'arrivée.

    Returns:
        Le nom de l'activité.
    """
    if trip.is_round_trip:
        return f"{trip.default_name()} — boucle depuis {departure.name}"
    return f"{trip.default_name()} — {departure.name} → {arrival.name}"


def build_activity_description(
    trip: VelibTrip, route: Route, settings: config.Config
) -> str:
    """Compose la description de l'activité Strava.

    Args:
        trip: Trajet concerné.
        route: Itinéraire calculé.
        settings: Configuration de l'exécution.

    Returns:
        La description complète.
    """
    details = []
    if route.distance_meters:
        details.append(f"{route.distance_meters / 1000:.2f} km")
    details.append(f"{trip.duration_seconds // 60} min")
    if trip.bike_type == "electrical":
        details.append("vélo électrique")
    elif trip.bike_type == "mechanical":
        details.append("vélo mécanique")
    if route.source == "fallback":
        details.append("tracé approché en ligne droite")

    return strava.build_description(settings.repo_url, extra=" · ".join(details))


def process_trip(
    trip: VelibTrip,
    catalog: routing.StationCatalog,
    access_token: str,
    settings: config.Config,
) -> tuple[str | None, str]:
    """Traite un trajet de bout en bout : itinéraire, GPX, envoi Strava.

    Args:
        trip: Trajet à synchroniser.
        catalog: Catalogue des stations.
        access_token: Jeton d'accès Strava, ou chaîne vide en simulation.
        settings: Configuration de l'exécution.

    Returns:
        Le couple (identifiant de l'activité Strava ou None, note de contexte).

    Raises:
        TripSkipped: Si le trajet ne peut pas être reconstitué.
        strava.StravaRateLimitError: Si le quota Strava est épuisé.
        strava.StravaError: En cas d'échec d'envoi.
    """
    departure, arrival = resolve_stations(trip, catalog)

    if trip.is_round_trip:
        # Une boucle assez longue est un vrai trajet, mais OpenRouteService
        # renverrait un itinéraire vide entre deux points identiques.
        raise TripSkipped(
            f"Boucle de {trip.duration_seconds // 60} min depuis {departure.name} : "
            "itinéraire indéterminable (départ et arrivée confondus)."
        )

    route = routing.get_route_coordinates(
        departure.coordinates, arrival.coordinates, settings.ors_api_key
    )
    time.sleep(ORS_DELAY_SECONDS)
    _verifier_coherence_distance(trip, route, departure, arrival)

    activity_name = build_activity_name(trip, departure, arrival)
    gpx_path = gpx_builder.build_gpx(
        route_points=route.points,
        start_time=trip.start_time,
        duration_seconds=trip.duration_seconds,
        track_name=activity_name,
        output_path=settings.gpx_file,
    )

    description = build_activity_description(trip, route, settings)

    if settings.dry_run:
        logger.info(
            "[simulation] %s — %d points, %s, aucun envoi effectué.",
            activity_name, len(route), route.source,
        )
        return None, f"simulation ({route.source})"

    result = strava.upload_to_strava(
        access_token=access_token,
        gpx_file_path=gpx_path,
        trip_name=activity_name,
        description=description,
        activity_type="ride",
    )
    time.sleep(STRAVA_DELAY_SECONDS)

    activity_id = result.get("activity_id")
    if activity_id:
        _verifier_activite_creee(access_token, activity_id, trip, route)
    return (str(activity_id) if activity_id else None), route.source


def run(settings: config.Config) -> int:
    """Exécute une synchronisation complète.

    Args:
        settings: Configuration validée.

    Returns:
        Un code de sortie à transmettre au shell.
    """
    state = ProcessedTripsState.load(settings.state_file)

    try:
        trips = velib.get_new_velib_trips(settings.velib_username, settings.velib_password)
    except velib.CloudflareChallenge as exc:
        logger.error("Blocage anti-bot : %s", exc)
        return EXIT_ANTIBOT
    except velib.VelibError as exc:
        logger.error("Récupération de l'historique Vélib' impossible : %s", exc)
        return EXIT_FAILURE

    candidates = select_trips(trips, state, settings)
    if not candidates:
        logger.info("Aucun nouveau trajet à synchroniser.")
        return EXIT_OK

    try:
        catalog = routing.StationCatalog.load(cache_path=STATIONS_CACHE)
    except routing.RoutingError as exc:
        logger.error("Géolocalisation des stations impossible : %s", exc)
        return EXIT_FAILURE

    if not catalog.is_authoritative:
        logger.warning(
            "Catalogue non autoritaire (source : %s, périmé : %s). Les trajets "
            "dont la station est introuvable seront reportés plutôt que perdus.",
            catalog.source, catalog.is_stale,
        )

    access_token = ""
    if settings.dry_run:
        logger.warning("Mode simulation : aucun envoi ne sera effectué vers Strava.")
    else:
        try:
            access_token = strava.get_access_token(
                settings.strava_client_id,
                settings.strava_client_secret,
                settings.strava_refresh_token,
            )
        except strava.StravaScopeError as exc:
            logger.error("%s", exc)
            return EXIT_CONFIG
        except strava.StravaError as exc:
            logger.error("Authentification Strava impossible : %s", exc)
            return EXIT_FAILURE

    uploaded = skipped = failed = deferred = 0

    for index, trip in enumerate(candidates, start=1):
        logger.info(
            "[%d/%d] Trajet %s du %s (%d min).",
            index, len(candidates), trip.trip_id,
            trip.start_time.strftime("%d/%m/%Y %H:%M UTC"),
            trip.duration_seconds // 60,
        )
        try:
            activity_id, note = process_trip(trip, catalog, access_token, settings)
        except CatalogDegraded as exc:
            # Inutile d'essayer les trajets suivants : ils échoueront tous pour
            # la même raison. On s'arrête sans rien marquer.
            logger.error(
                "%s Arrêt : aucun trajet ne peut être géolocalisé tant que "
                "l'open data Smovengo est injoignable.", exc
            )
            failed += 1
            break
        except TripDeferred as exc:
            # Lacune ponctuelle du catalogue : on passe au trajet suivant sans
            # rien marquer, pour le retenter quand Smovengo répondra.
            logger.warning("Trajet %s reporté : %s", trip.trip_id, exc)
            deferred += 1
        except TripSkipped as exc:
            # Trajet inexploitable de façon définitive : on le marque traité pour
            # ne pas le réexaminer à chaque exécution.
            logger.warning("Trajet %s écarté : %s", trip.trip_id, exc)
            state.mark_processed(trip, note=f"écarté : {exc}")
            skipped += 1
        except strava.StravaDuplicateError as exc:
            # L'activité est déjà sur Strava : l'objectif est atteint.
            logger.info("Trajet %s déjà présent sur Strava : %s", trip.trip_id, exc)
            state.mark_processed(trip, note="doublon détecté par Strava")
            skipped += 1
        except strava.StravaRateLimitError as exc:
            logger.error("%s Arrêt de l'exécution, reprise demain.", exc)
            break
        except (routing.RoutingError, gpx_builder.GpxBuildError, strava.StravaError) as exc:
            # Échec potentiellement transitoire : ne PAS marquer le trajet
            # comme traité, pour lui laisser sa chance à la prochaine exécution.
            logger.error("Trajet %s en échec : %s", trip.trip_id, exc)
            failed += 1
        else:
            if settings.dry_run:
                logger.info("Trajet %s simulé (%s).", trip.trip_id, note)
                skipped += 1
            else:
                state.mark_processed(trip, activity_id=activity_id, note=note)
                uploaded += 1
                logger.info(
                    "Trajet %s envoyé (activité Strava %s).", trip.trip_id, activity_id
                )
        finally:
            # Écriture après chaque trajet : une interruption ne fait pas perdre
            # le travail déjà accompli.
            try:
                state.save()
            except OSError as exc:
                logger.error("Écriture de l'état impossible : %s", exc)

    logger.info(
        "Bilan : %d envoyés, %d écartés, %d en échec, %d reportés.",
        uploaded, skipped, failed, deferred,
    )
    if deferred:
        logger.info(
            "Les %d trajets reportés le sont faute de station connue du cache : "
            "ils repartiront d'eux-mêmes dès que l'open data Smovengo répondra.",
            deferred,
        )

    # Un échec transitoire isolé ne doit pas faire échouer le workflow entier :
    # seul un échec général (aucun succès alors qu'il y avait du travail) le
    # fait. Un report n'est pas un échec : le trajet n'est pas perdu, il attend
    # que Smovengo revienne.
    if failed and not uploaded and not skipped:
        return EXIT_FAILURE
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Point d'entrée du script.

    Args:
        argv: Arguments de ligne de commande (None = `sys.argv`).

    Returns:
        Le code de sortie.
    """
    parser = argparse.ArgumentParser(
        description="Synchronise les trajets Vélib' Métropole vers Strava."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Construit les GPX sans rien envoyer sur Strava.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Journalisation détaillée (niveau DEBUG).",
    )
    parser.add_argument(
        "--max-trips", type=int, default=None,
        help="Plafonne le nombre de trajets traités lors de cette exécution.",
    )
    args = parser.parse_args(argv)

    configure_logging(args.verbose)

    try:
        settings = config.load_config()
    except config.ConfigError as exc:
        logger.error("Configuration invalide : %s", exc)
        return EXIT_CONFIG

    # Les options de ligne de commande priment sur l'environnement.
    overrides = {}
    if args.dry_run:
        overrides["dry_run"] = True
    if args.max_trips is not None:
        overrides["max_trips_per_run"] = args.max_trips
    if overrides:
        from dataclasses import replace
        settings = replace(settings, **overrides)

    try:
        return run(settings)
    except KeyboardInterrupt:
        logger.warning("Interruption demandée par l'utilisateur.")
        return EXIT_FAILURE
    except Exception as exc:  # filet de sécurité pour l'exécution en CI
        logger.exception("Erreur inattendue : %s : %s", type(exc).__name__, exc)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
