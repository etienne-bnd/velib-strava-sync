#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Structures de données partagées par tous les modules du projet.

Ce module est volontairement dépourvu de dépendances externes : il ne décrit
que la forme des données qui circulent entre `velib`, `routing`, `gpx_builder`
et `strava`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Un point GPS au format (longitude, latitude), l'ordre retenu par
# OpenRouteService et par le GeoJSON en général. On conserve cet ordre de bout
# en bout pour éviter les inversions silencieuses ; seule la construction du
# GPX repasse en (latitude, longitude).
Coordinates = tuple[float, float]


@dataclass(frozen=True)
class Station:
    """Une station Vélib' issue de l'open data Smovengo."""

    station_id: str
    name: str
    longitude: float
    latitude: float

    @property
    def coordinates(self) -> Coordinates:
        """Coordonnées au format (longitude, latitude)."""
        return (self.longitude, self.latitude)


@dataclass
class VelibTrip:
    """Un trajet Vélib' normalisé, extrait de `getCourseList`.

    Attributes:
        trip_id: Identifiant stable du trajet, utilisé comme clé de déduplication.
        start_time: Horodatage de départ, toujours en UTC (timezone-aware).
        duration_seconds: Durée du trajet en secondes.
        departure_station_id: Identifiant de la station de départ.
        arrival_station_id: Identifiant de la station d'arrivée.
        departure_station_name: Libellé de la station de départ, si fourni.
        arrival_station_name: Libellé de la station d'arrivée, si fourni.
        bike_type: « mechanical », « electrical » ou None si inconnu.
        distance_meters: Distance annoncée par Vélib', si disponible.
        raw: Charge utile JSON d'origine, conservée pour le diagnostic.
    """

    trip_id: str
    start_time: datetime
    duration_seconds: int
    departure_station_id: str | None = None
    arrival_station_id: str | None = None
    departure_station_name: str | None = None
    arrival_station_name: str | None = None
    bike_type: str | None = None
    distance_meters: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Garantit que `start_time` est toujours conscient du fuseau et en UTC."""
        if self.start_time.tzinfo is None:
            self.start_time = self.start_time.replace(tzinfo=timezone.utc)
        else:
            self.start_time = self.start_time.astimezone(timezone.utc)

    @property
    def is_round_trip(self) -> bool:
        """True si le trajet part et arrive à la même station."""
        return (
            self.departure_station_id is not None
            and self.departure_station_id == self.arrival_station_id
        )

    @property
    def is_probable_cancellation(self) -> bool:
        """True si le trajet ressemble à une location annulée.

        Vélib' facture un trajet dès le déverrouillage. Reposer le vélo à la
        même borne en moins de deux minutes correspond en pratique à une
        annulation (vélo défectueux, erreur de manipulation) : il n'y a aucun
        déplacement à reconstituer.
        """
        return self.is_round_trip and self.duration_seconds <= 120

    def default_name(self) -> str:
        """Nom d'activité Strava par défaut, dépendant du moment de la journée."""
        hour = self.start_time.hour
        if 5 <= hour < 11:
            moment = "matinal"
        elif 11 <= hour < 14:
            moment = "de midi"
        elif 14 <= hour < 18:
            moment = "de l'après-midi"
        elif 18 <= hour < 22:
            moment = "du soir"
        else:
            moment = "nocturne"
        return f"Vélib' {moment}"


@dataclass
class Route:
    """Un itinéraire cyclable calculé entre deux stations.

    Attributes:
        points: Suite ordonnée de points (longitude, latitude).
        distance_meters: Longueur de l'itinéraire selon le moteur de routage.
        source: « openrouteservice » si calculé, « fallback » si interpolé.
    """

    points: list[Coordinates]
    distance_meters: float | None = None
    source: str = "openrouteservice"

    def __len__(self) -> int:
        return len(self.points)
