#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Géolocalisation des stations Vélib' et calcul d'itinéraires cyclables.

Deux sources sont mobilisées :

* l'open data Smovengo (`station_information.json`) pour convertir un
  identifiant de station en coordonnées GPS ;
* OpenRouteService (`/v2/directions/cycling-regular`) pour tracer le chemin
  cyclable réel entre deux stations.

En cas d'indisponibilité d'OpenRouteService, un repli par interpolation en
ligne droite permet de ne pas perdre le trajet ; le tracé est alors moins
fidèle, ce que `Route.source` signale.
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Any

import requests

from models import Coordinates, Route, Station

logger = logging.getLogger(__name__)

STATION_INFORMATION_URL = (
    "https://velib-metropole-opendata.smoove.pro/opendata/Velib_Metropole/"
    "station_information.json"
)
# Source miroir : le portail Paris Open Data republie le même référentiel de
# stations. Il sert de secours quand smoove.pro est indisponible — ce qui arrive
# régulièrement — ou inatteignable depuis le réseau appelant.
PARIS_OPENDATA_URL = (
    "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets/"
    "velib-disponibilite-en-temps-reel/records"
)
PARIS_OPENDATA_PAGE_SIZE = 100  # maximum autorisé par l'API Explore v2.1

ORS_DIRECTIONS_URL = "https://api.openrouteservice.org/v2/directions/cycling-regular"

REQUEST_TIMEOUT = 30      # secondes
ORS_MAX_RETRIES = 3
ORS_RETRY_BACKOFF = 5     # secondes, multiplié par le numéro de tentative
FALLBACK_POINT_COUNT = 40  # points générés par le repli en ligne droite

EARTH_RADIUS_METERS = 6_371_000.0


class RoutingError(RuntimeError):
    """Erreur de géolocalisation ou de calcul d'itinéraire."""


class StationNotFoundError(RoutingError):
    """Aucune station de l'open data ne correspond à l'identifiant demandé."""


# --------------------------------------------------------------------------- #
# Géométrie
# --------------------------------------------------------------------------- #

def haversine_distance(start: Coordinates, end: Coordinates) -> float:
    """Distance orthodromique entre deux points, en mètres.

    Args:
        start: Point de départ (longitude, latitude).
        end: Point d'arrivée (longitude, latitude).

    Returns:
        La distance en mètres.
    """
    lon1, lat1 = math.radians(start[0]), math.radians(start[1])
    lon2, lat2 = math.radians(end[0]), math.radians(end[1])
    delta_lon, delta_lat = lon2 - lon1, lat2 - lat1
    a = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(a))


def path_length(points: list[Coordinates]) -> float:
    """Longueur cumulée d'une polyligne, en mètres."""
    return sum(
        haversine_distance(points[index], points[index + 1])
        for index in range(len(points) - 1)
    )


def _fetch_paris_opendata_records() -> list[dict[str, Any]]:
    """Récupère toutes les stations du miroir Paris Open Data, page par page.

    Returns:
        La liste des enregistrements bruts.

    Raises:
        requests.RequestException: En cas d'échec réseau.
        RoutingError: Si la pagination ne retourne rien.
    """
    records: list[dict[str, Any]] = []
    offset = 0
    total: int | None = None

    while True:
        response = requests.get(
            PARIS_OPENDATA_URL,
            params={"limit": PARIS_OPENDATA_PAGE_SIZE, "offset": offset},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()

        batch = payload.get("results") or []
        records.extend(batch)
        total = payload.get("total_count", total)

        if not batch or (total is not None and len(records) >= total):
            break
        offset += PARIS_OPENDATA_PAGE_SIZE
        # L'API Explore plafonne l'accès anonyme à 10 000 enregistrements.
        if offset >= 10_000:
            break

    if not records:
        raise RoutingError("Le miroir Paris Open Data n'a retourné aucune station.")
    return records


# --------------------------------------------------------------------------- #
# Catalogue des stations
# --------------------------------------------------------------------------- #

class StationCatalog:
    """Index des stations Vélib', interrogeable par identifiant.

    L'open data expose deux identifiants par station : `station_id` (numérique,
    interne à Smovengo) et `stationCode` (le code à cinq chiffres affiché en
    borne). L'API privée Vélib' emploie tantôt l'un, tantôt l'autre selon les
    champs : les deux sont donc indexés.
    """

    #: Sources possibles, par ordre de complétude décroissante.
    SOURCE_SMOVENGO = "smovengo"
    SOURCE_PARIS_OPENDATA = "paris_opendata"

    #: Complétude relative des sources. Elle ordonne deux décisions : quel repli
    #: choisir quand Smovengo est injoignable, et quelle écriture de cache
    #: refuser. Smovengo domine parce qu'il est le SEUL à publier les
    #: identifiants internes de station, ceux que l'API privée Vélib' emploie.
    SOURCE_RANK = {SOURCE_SMOVENGO: 2, SOURCE_PARIS_OPENDATA: 1}

    def __init__(
        self,
        stations: dict[str, Station],
        source: str = SOURCE_SMOVENGO,
        is_stale: bool = False,
    ) -> None:
        """Args:
            stations: Index identifiant -> station, déjà construit.
            source: Origine des données, qui détermine les identifiants résolus.
            is_stale: True si les données viennent d'un cache périmé, donc
                potentiellement amputé des stations créées depuis.
        """
        self._stations = stations
        self.source = source
        self.is_stale = is_stale

    @property
    def is_authoritative(self) -> bool:
        """True si une station absente du catalogue l'est réellement du réseau.

        Un catalogue frais issu de Smovengo fait autorité : une station qu'il
        ignore a été supprimée. Un catalogue périmé, ou issu du miroir, ne
        permet pas cette conclusion — l'absence peut n'être qu'une lacune.
        """
        return self.resolves_internal_ids and not self.is_stale

    @property
    def resolves_internal_ids(self) -> bool:
        """True si le catalogue sait résoudre les identifiants internes Smovengo.

        L'API privée Vélib' désigne les stations par leur identifiant interne
        (« 128920403 »), que seul l'open data Smovengo publie. Le miroir Paris
        Open Data n'expose que le code à cinq chiffres affiché en borne
        (« 16107 ») : un catalogue construit depuis ce miroir ne peut résoudre
        aucun trajet réel.
        """
        return self.source == self.SOURCE_SMOVENGO

    def __len__(self) -> int:
        # Une même station apparaît sous plusieurs clés : on compte les objets
        # distincts, pas les entrées d'index.
        return len({station.station_id for station in self._stations.values()})

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> StationCatalog:
        """Construit le catalogue à partir de la charge utile GBFS.

        Args:
            payload: Contenu de `station_information.json`.

        Returns:
            Le catalogue indexé.

        Raises:
            RoutingError: Si la charge utile ne contient aucune station.
        """
        raw_stations = payload.get("data", {}).get("stations", [])
        if not raw_stations:
            raise RoutingError(
                "L'open data Smovengo n'a retourné aucune station "
                "(structure data.stations vide ou absente)."
            )

        index: dict[str, Station] = {}
        for raw in raw_stations:
            try:
                station = Station(
                    station_id=str(raw["station_id"]),
                    name=str(raw.get("name", "Station inconnue")),
                    longitude=float(raw["lon"]),
                    latitude=float(raw["lat"]),
                )
            except (KeyError, TypeError, ValueError):
                logger.debug("Station ignorée, champs incomplets : %r", raw)
                continue

            for key in (raw.get("station_id"), raw.get("stationCode")):
                if key not in (None, ""):
                    index[str(key)] = station

        if not index:
            raise RoutingError("Aucune station exploitable dans l'open data Smovengo.")
        return cls(index, source=cls.SOURCE_SMOVENGO)

    @classmethod
    def from_paris_opendata(cls, records: list[dict[str, Any]]) -> StationCatalog:
        """Construit le catalogue depuis les enregistrements du miroir Paris Open Data.

        Ce miroir n'expose que le code à cinq chiffres (`stationcode`), pas
        l'identifiant numérique interne de Smovengo. C'est suffisant : l'API
        privée Vélib' désigne les stations par ce même code.

        Args:
            records: Enregistrements retournés par l'API Explore v2.1.

        Returns:
            Le catalogue indexé.

        Raises:
            RoutingError: Si aucun enregistrement n'est exploitable.
        """
        index: dict[str, Station] = {}
        for raw in records:
            coords = raw.get("coordonnees_geo") or {}
            code = raw.get("stationcode")
            if code in (None, "") or "lon" not in coords or "lat" not in coords:
                continue
            try:
                station = Station(
                    station_id=str(code),
                    name=str(raw.get("name", "Station inconnue")),
                    longitude=float(coords["lon"]),
                    latitude=float(coords["lat"]),
                )
            except (TypeError, ValueError):
                continue
            index[str(code)] = station

        if not index:
            raise RoutingError("Aucune station exploitable dans le miroir Paris Open Data.")
        return cls(index, source=cls.SOURCE_PARIS_OPENDATA)

    @classmethod
    def load(
        cls, cache_path: Path | str | None = None, max_cache_age_seconds: int = 86_400
    ) -> StationCatalog:
        """Charge le catalogue depuis le cache disque, sinon depuis l'open data.

        Args:
            cache_path: Fichier de cache local. Aucun cache si None.
            max_cache_age_seconds: Durée de validité du cache.

        Returns:
            Le catalogue indexé.

        Raises:
            RoutingError: Si le téléchargement échoue et qu'aucun cache n'est
                utilisable.
        """
        cache = Path(cache_path) if cache_path else None

        # Le cache est lu une seule fois et conservé : il sert deux fois dans la
        # cascade de replis ci-dessous, et le relire coûterait un second
        # décodage de plusieurs centaines de kilo-octets.
        cached_payload: dict[str, Any] | None = None

        if cache and cache.is_file():
            age = time.time() - cache.stat().st_mtime
            try:
                cached_payload = json.loads(cache.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning("Cache de stations illisible (%s), re-téléchargement.", exc)

            if cached_payload is not None and age < max_cache_age_seconds:
                try:
                    catalog = cls.from_cached_payload(cached_payload)
                    logger.info("Catalogue chargé depuis le cache (%d stations).", len(catalog))
                    return catalog
                except RoutingError as exc:
                    logger.warning("Cache de stations inexploitable (%s), re-téléchargement.", exc)
            elif cached_payload is not None:
                logger.info(
                    "Cache de stations périmé (%.1f jour(s)) : tentative de "
                    "rafraîchissement.", age / 86_400,
                )

        # Source principale : l'open data Smovengo, format GBFS.
        try:
            response = requests.get(STATION_INFORMATION_URL, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
            catalog = cls.from_payload(payload)
            logger.info("Catalogue téléchargé depuis Smovengo (%d stations).", len(catalog))
            cls._write_cache(cache, payload)
            return catalog
        except (requests.RequestException, ValueError, RoutingError) as exc:
            logger.warning(
                "Open data Smovengo indisponible (%s), recherche d'un repli.", exc
            )
            primary_error = exc

        # Premier repli : un cache Smovengo PÉRIMÉ, avant le miroir.
        #
        # L'ordre importe et il est contre-intuitif : on préfère ici des données
        # vieilles de plusieurs jours à des données fraîches. C'est que les deux
        # sources ne sont pas interchangeables. L'API privée Vélib' désigne ses
        # stations par identifiant interne, que seul Smovengo publie ; le miroir
        # n'expose que le code à cinq chiffres. Or les coordonnées d'une station
        # ne bougent pratiquement jamais : la péremption ne coûte que les
        # stations créées depuis, tandis que le miroir coûte la résolution de
        # TOUS les trajets. Un cache périmé qui résout les trajets vaut donc
        # mieux qu'un catalogue frais qui n'en résout aucun.
        if cached_payload is not None and (
            cls._payload_source(cached_payload) == cls.SOURCE_SMOVENGO
        ):
            try:
                catalog = cls.from_cached_payload(cached_payload)
            except RoutingError as exc:
                logger.warning("Cache Smovengo périmé inexploitable (%s).", exc)
            else:
                catalog.is_stale = True
                logger.warning(
                    "Smovengo injoignable : repli sur le cache Smovengo périmé "
                    "(%d stations). Il résout les identifiants internes, ce que "
                    "le miroir Paris Open Data ne sait pas faire. Les stations "
                    "créées depuis sa constitution resteront introuvables ; les "
                    "trajets concernés seront reportés, pas perdus.",
                    len(catalog),
                )
                return catalog

        # Second repli : le miroir Paris Open Data, au format Explore v2.1. Il
        # n'est atteint qu'en l'absence de tout cache Smovengo exploitable.
        try:
            records = _fetch_paris_opendata_records()
            catalog = cls.from_paris_opendata(records)
            logger.warning(
                "Catalogue de secours (miroir Paris Open Data, %d stations) : ce "
                "miroir n'expose QUE les codes à cinq chiffres. Les trajets Vélib' "
                "désignant leurs stations par identifiant interne ne pourront pas "
                "être géolocalisés tant que Smovengo reste injoignable.",
                len(catalog),
            )
            cls._write_cache(cache, {"paris_opendata_records": records})
            return catalog
        except (requests.RequestException, ValueError, RoutingError) as exc:
            logger.warning("Miroir Paris Open Data indisponible (%s).", exc)

        # Dernier recours : un cache périmé vaut mieux qu'un échec total. Après
        # le repli ci-dessus, il ne peut plus s'agir que d'un cache issu du
        # miroir — les trajets désignés par identifiant interne y resteront
        # introuvables, mais ceux désignés par code à cinq chiffres passeront.
        if cached_payload is not None:
            logger.warning(
                "Toutes les sources en ligne sont indisponibles : repli sur le "
                "cache de stations périmé (source %s). Les trajets dont la "
                "station est introuvable seront reportés, pas perdus.",
                cls._payload_source(cached_payload),
            )
            try:
                catalog = cls.from_cached_payload(cached_payload)
                catalog.is_stale = True
                return catalog
            except RoutingError:
                pass

        raise RoutingError(
            "Aucune source de géolocalisation des stations n'est accessible "
            f"(erreur initiale : {primary_error})."
        )

    @classmethod
    def from_cached_payload(cls, payload: dict[str, Any]) -> StationCatalog:
        """Reconstruit le catalogue depuis un cache, quelle qu'en soit la source."""
        if "paris_opendata_records" in payload:
            return cls.from_paris_opendata(payload["paris_opendata_records"])
        return cls.from_payload(payload)

    @classmethod
    def _payload_source(cls, payload: dict[str, Any]) -> str:
        """Déduit la source d'une charge utile de cache à sa structure.

        Le cache ne porte pas d'étiquette de provenance : la forme suffit à la
        déterminer, le miroir étant stocké sous une clé qui lui est propre.

        Args:
            payload: Contenu déjà désérialisé du cache.

        Returns:
            `SOURCE_PARIS_OPENDATA` ou `SOURCE_SMOVENGO`.
        """
        if "paris_opendata_records" in payload:
            return cls.SOURCE_PARIS_OPENDATA
        return cls.SOURCE_SMOVENGO

    @classmethod
    def _write_cache(cls, cache: Path | None, payload: dict[str, Any]) -> None:
        """Écrit le cache disque, sauf si cela dégraderait son contenu.

        Un cache Smovengo ne doit JAMAIS être remplacé par des données du
        miroir. Le cache est le référentiel de secours du projet — il est
        versionné dans le dépôt précisément parce que Smovengo est
        régulièrement injoignable — et seul Smovengo publie les identifiants
        internes de station. L'écraser par un jeu de données qui n'expose que
        les codes à cinq chiffres détruit la seule copie exploitable, et le
        workflow, qui commite ce fichier avec `if: always()`, rendrait la perte
        définitive.

        Constaté en conditions réelles le 9 septembre 2026 : une exécution
        pendant une panne Smovengo a remplacé 1471 stations avec identifiants
        internes par 1519 entrées sans, rendant tout trajet non géolocalisable.

        Args:
            cache: Fichier de cache, ou None pour ne rien écrire.
            payload: Charge utile à enregistrer.
        """
        if cache is None:
            return

        nouvelle_source = cls._payload_source(payload)
        if cache.is_file():
            try:
                existante = cls._payload_source(
                    json.loads(cache.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError):
                # Cache absent ou corrompu : rien à préserver, on écrit.
                existante = None
            if existante is not None and cls.SOURCE_RANK.get(
                nouvelle_source, 0
            ) < cls.SOURCE_RANK.get(existante, 0):
                logger.warning(
                    "Cache de stations NON réécrit : le conserver (source %s) "
                    "vaut mieux que le remplacer par des données %s, moins "
                    "complètes. Seul Smovengo publie les identifiants internes "
                    "de station.", existante, nouvelle_source,
                )
                return

        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            logger.warning("Écriture du cache de stations impossible : %s", exc)

    def get(self, station_id: str | int | None) -> Station | None:
        """Retourne la station correspondante, ou None si l'identifiant est inconnu.

        Les sources ne s'accordent pas sur les zéros initiaux : Smovengo écrit
        « 07025 », le miroir Paris Open Data « 7025 ». On essaie donc la clé
        telle quelle, puis sans ses zéros initiaux, puis complétée à cinq
        chiffres — les codes de station en comportent cinq.

        Args:
            station_id: Identifiant ou code de station.

        Returns:
            La station, ou None si aucune variante ne correspond.
        """
        if station_id in (None, ""):
            return None

        key = str(station_id).strip()
        depouille = key.lstrip("0")
        for variante in (key, depouille, depouille.zfill(5)):
            station = self._stations.get(variante)
            if station is not None:
                return station
        return None

    def require(self, station_id: str | int | None) -> Station:
        """Comme `get`, mais lève une erreur si la station est introuvable.

        Raises:
            StationNotFoundError: Si aucune station ne correspond.
        """
        station = self.get(station_id)
        if station is None:
            raise StationNotFoundError(
                f"Station {station_id!r} absente de l'open data Smovengo "
                "(station supprimée, ou identifiant d'un autre référentiel)."
            )
        return station


# --------------------------------------------------------------------------- #
# Calcul d'itinéraire
# --------------------------------------------------------------------------- #

def _interpolate_straight_line(
    start_coords: Coordinates, end_coords: Coordinates, point_count: int = FALLBACK_POINT_COUNT
) -> list[Coordinates]:
    """Génère une ligne droite entre deux points, en repli d'OpenRouteService.

    Args:
        start_coords: Départ (longitude, latitude).
        end_coords: Arrivée (longitude, latitude).
        point_count: Nombre total de points générés, extrémités comprises.

    Returns:
        La liste des points interpolés.
    """
    count = max(2, point_count)
    step = 1.0 / (count - 1)
    return [
        (
            start_coords[0] + (end_coords[0] - start_coords[0]) * index * step,
            start_coords[1] + (end_coords[1] - start_coords[1]) * index * step,
        )
        for index in range(count)
    ]


def get_route_coordinates(
    start_coords: Coordinates,
    end_coords: Coordinates,
    api_key: str,
    allow_fallback: bool = True,
) -> Route:
    """Calcule le chemin cyclable entre deux stations.

    Args:
        start_coords: Départ (longitude, latitude).
        end_coords: Arrivée (longitude, latitude).
        api_key: Clé de l'API OpenRouteService.
        allow_fallback: Si True, une ligne droite interpolée est retournée
            lorsque OpenRouteService échoue ; sinon l'erreur est propagée.

    Returns:
        L'itinéraire calculé. `Route.source` vaut « openrouteservice » ou
        « fallback ».

    Raises:
        RoutingError: Si le calcul échoue et que `allow_fallback` est False.
    """
    headers = {
        "Authorization": api_key,
        "Accept": "application/json, application/geo+json",
        "Content-Type": "application/json; charset=utf-8",
    }
    body = {
        "coordinates": [list(start_coords), list(end_coords)],
        # Le format GeoJSON évite d'avoir à décoder la polyligne encodée.
        "instructions": False,
    }

    last_error: Exception | None = None
    for attempt in range(1, ORS_MAX_RETRIES + 1):
        try:
            response = requests.post(
                f"{ORS_DIRECTIONS_URL}/geojson",
                json=body,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_error = exc
            logger.warning(
                "OpenRouteService injoignable : %s (tentative %d/%d).",
                exc, attempt, ORS_MAX_RETRIES,
            )
        else:
            if response.status_code == 200:
                try:
                    return _parse_ors_geojson(response.json())
                except RoutingError as exc:
                    last_error = exc
                    logger.warning("Réponse OpenRouteService inexploitable : %s", exc)
                    break  # une réponse 200 mal formée ne se corrigera pas au réessai

            if response.status_code == 429:
                # Quota atteint : le réessai n'a de sens qu'après une pause.
                last_error = RoutingError("Quota OpenRouteService atteint (HTTP 429).")
                logger.warning(
                    "Quota OpenRouteService atteint (tentative %d/%d).",
                    attempt, ORS_MAX_RETRIES,
                )
            elif response.status_code in (401, 403):
                # Clé invalide : inutile d'insister.
                raise RoutingError(
                    f"Clé OpenRouteService refusée (HTTP {response.status_code}). "
                    "Vérifier ORS_API_KEY."
                )
            else:
                last_error = RoutingError(
                    f"OpenRouteService a renvoyé HTTP {response.status_code} : "
                    f"{response.text[:300]}"
                )
                logger.warning("%s", last_error)

        if attempt < ORS_MAX_RETRIES:
            time.sleep(ORS_RETRY_BACKOFF * attempt)

    if not allow_fallback:
        raise RoutingError(f"Calcul d'itinéraire impossible : {last_error}")

    logger.warning(
        "Repli sur une interpolation en ligne droite (cause : %s).", last_error
    )
    points = _interpolate_straight_line(start_coords, end_coords)
    return Route(points=points, distance_meters=path_length(points), source="fallback")


def _parse_ors_geojson(payload: dict[str, Any]) -> Route:
    """Extrait les points et la distance d'une réponse GeoJSON d'OpenRouteService.

    Args:
        payload: Corps JSON de la réponse.

    Returns:
        L'itinéraire correspondant.

    Raises:
        RoutingError: Si la structure attendue est absente ou vide.
    """
    features = payload.get("features") or []
    if not features:
        raise RoutingError("Réponse OpenRouteService sans entité « features ».")

    geometry = features[0].get("geometry") or {}
    raw_points = geometry.get("coordinates") or []
    if len(raw_points) < 2:
        raise RoutingError(
            f"Itinéraire OpenRouteService trop court ({len(raw_points)} point(s))."
        )

    points: list[Coordinates] = [
        (float(point[0]), float(point[1])) for point in raw_points
    ]
    summary = (features[0].get("properties") or {}).get("summary") or {}
    distance = summary.get("distance")

    return Route(
        points=points,
        distance_meters=float(distance) if isinstance(distance, (int, float)) else path_length(points),
        source="openrouteservice",
    )
