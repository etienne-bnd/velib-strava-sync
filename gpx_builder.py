#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Génération du fichier GPX horodaté à partir d'un itinéraire.

Le problème à résoudre : l'API Vélib' fournit une heure de départ et une durée
totale, OpenRouteService fournit une géométrie sans aucun horodatage. Il faut
donc synthétiser un timestamp par point.

La répartition retenue est proportionnelle à la **distance** de chaque segment,
et non à son indice. OpenRouteService densifie les points dans les virages et
les espace dans les lignes droites : répartir le temps par indice donnerait
une vitesse instantanée très irrégulière — quasi nulle dans les virages, avec
des pointes dans les lignes droites — que Strava afficherait comme un profil
de vitesse aberrant. Une répartition par distance produit exactement la vitesse
moyenne constante demandée.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gpxpy
import gpxpy.gpx

from models import Coordinates
from routing import haversine_distance

logger = logging.getLogger(__name__)

GPX_CREATOR = "velibsurstrava"


class GpxBuildError(RuntimeError):
    """Les données fournies ne permettent pas de construire un GPX valide."""


def compute_cumulative_distances(points: list[Coordinates]) -> list[float]:
    """Calcule la distance cumulée depuis le départ, pour chaque point.

    Args:
        points: Suite ordonnée de points (longitude, latitude).

    Returns:
        Une liste de même longueur que `points`, commençant par 0.0.
    """
    cumulative = [0.0]
    for index in range(1, len(points)):
        cumulative.append(
            cumulative[-1] + haversine_distance(points[index - 1], points[index])
        )
    return cumulative


def compute_timestamps(
    points: list[Coordinates], start_time: datetime, duration_seconds: int
) -> list[datetime]:
    """Attribue un horodatage UTC à chaque point de l'itinéraire.

    L'instant d'un point est proportionnel à sa distance parcourue depuis le
    départ, ce qui donne une vitesse moyenne constante sur tout le tracé.

    Args:
        points: Suite ordonnée de points (longitude, latitude).
        start_time: Instant de départ.
        duration_seconds: Durée totale du trajet, en secondes.

    Returns:
        Les horodatages, en UTC, dans l'ordre des points.

    Raises:
        GpxBuildError: Si l'itinéraire compte moins de deux points ou si la
            durée n'est pas strictement positive.
    """
    if len(points) < 2:
        raise GpxBuildError(
            f"Un itinéraire d'au moins deux points est requis, reçu {len(points)}."
        )
    if duration_seconds <= 0:
        raise GpxBuildError(
            f"La durée doit être strictement positive, reçu {duration_seconds}."
        )

    start_utc = (
        start_time.replace(tzinfo=timezone.utc)
        if start_time.tzinfo is None
        else start_time.astimezone(timezone.utc)
    )

    cumulative = compute_cumulative_distances(points)
    total_distance = cumulative[-1]

    if total_distance <= 0:
        # Tous les points sont confondus (station de départ et d'arrivée
        # identiques, itinéraire dégénéré) : on retombe sur une répartition
        # par indice, seule option restante.
        logger.warning(
            "Itinéraire de longueur nulle : répartition du temps par indice."
        )
        step = duration_seconds / (len(points) - 1)
        return [start_utc + timedelta(seconds=step * i) for i in range(len(points))]

    return [
        start_utc + timedelta(seconds=duration_seconds * distance / total_distance)
        for distance in cumulative
    ]


def build_gpx(
    route_points: list[Coordinates],
    start_time: datetime,
    duration_seconds: int,
    track_name: str = "Trajet Vélib'",
    output_path: Path | str = "temp_trip.gpx",
) -> Path:
    """Construit le fichier GPX horodaté et l'écrit sur disque.

    Args:
        route_points: Points de l'itinéraire, au format (longitude, latitude).
        start_time: Instant de départ du trajet.
        duration_seconds: Durée totale du trajet, en secondes.
        track_name: Nom de la trace inscrit dans le GPX.
        output_path: Chemin du fichier à écrire.

    Returns:
        Le chemin du fichier écrit.

    Raises:
        GpxBuildError: Si les données sont invalides ou l'écriture impossible.
    """
    timestamps = compute_timestamps(route_points, start_time, duration_seconds)

    gpx = gpxpy.gpx.GPX()
    gpx.creator = GPX_CREATOR

    track = gpxpy.gpx.GPXTrack(name=track_name)
    # « 1 » est le code Garmin pour le vélo de route ; Strava s'appuie surtout
    # sur le paramètre activity_type de l'envoi, mais un GPX cohérent facilite
    # la réutilisation du fichier ailleurs.
    track.type = "cycling"
    gpx.tracks.append(track)

    segment = gpxpy.gpx.GPXTrackSegment()
    track.segments.append(segment)

    for (longitude, latitude), timestamp in zip(route_points, timestamps):
        segment.points.append(
            gpxpy.gpx.GPXTrackPoint(
                latitude=latitude,      # attention : le GPX inverse l'ordre
                longitude=longitude,
                time=timestamp,
            )
        )

    destination = Path(output_path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(gpx.to_xml(), encoding="utf-8")
    except OSError as exc:
        raise GpxBuildError(f"Écriture de {destination} impossible : {exc}") from exc

    logger.info(
        "GPX écrit : %s (%d points, %d s).",
        destination, len(route_points), duration_seconds,
    )
    return destination
