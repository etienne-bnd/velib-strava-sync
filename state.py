#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gestion du fichier d'état `processed_trips.json`.

Ce fichier est la mémoire du projet entre deux exécutions : il recense les
trajets déjà envoyés sur Strava, pour ne jamais créer de doublon. Le workflow
GitHub Actions le commite après chaque exécution.

Deux clés sont enregistrées par trajet : l'identifiant Vélib' et une empreinte
dérivée de la date de départ. La seconde couvre le cas où l'API changerait sa
manière de numéroter les trajets — l'identifiant serait alors différent, mais
l'horodatage de départ, lui, ne bouge pas.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models import VelibTrip

logger = logging.getLogger(__name__)

STATE_VERSION = 1


def time_key(start_time: datetime) -> str:
    """Construit la clé de repli d'un trajet à partir de son heure de départ.

    Args:
        start_time: Instant de départ du trajet.

    Returns:
        Une chaîne ISO 8601 en UTC, à la seconde près.
    """
    moment = (
        start_time.replace(tzinfo=timezone.utc)
        if start_time.tzinfo is None
        else start_time.astimezone(timezone.utc)
    )
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class ProcessedTripsState:
    """État persistant des trajets déjà synchronisés."""

    def __init__(self, path: Path | str, entries: dict[str, dict[str, Any]] | None = None) -> None:
        """Args:
            path: Chemin du fichier d'état.
            entries: Entrées déjà chargées, indexées par clé.
        """
        self.path = Path(path)
        self._entries: dict[str, dict[str, Any]] = entries or {}
        self._dirty = False

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def dirty(self) -> bool:
        """True si des modifications restent à écrire sur disque."""
        return self._dirty

    @classmethod
    def load(cls, path: Path | str) -> ProcessedTripsState:
        """Charge l'état depuis le disque.

        Un fichier absent donne un état vide — c'est le cas de la toute première
        exécution. Un fichier corrompu est signalé mais ne bloque pas : repartir
        d'un état vide ne provoque au pire que des doublons, que Strava rejette
        de lui-même.

        Args:
            path: Chemin du fichier d'état.

        Returns:
            L'état chargé.
        """
        state_path = Path(path)
        if not state_path.is_file():
            logger.info("Aucun fichier d'état à %s : première exécution.", state_path)
            return cls(state_path)

        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.error(
                "Fichier d'état %s illisible (%s) : reprise sur un état vide. "
                "Strava rejettera les éventuels doublons.", state_path, exc
            )
            return cls(state_path)

        entries = cls._normalise_payload(payload)
        logger.info("État chargé : %d trajets déjà synchronisés.", len(entries))
        return cls(state_path, entries)

    @staticmethod
    def _normalise_payload(payload: Any) -> dict[str, dict[str, Any]]:
        """Convertit le contenu du fichier en index de clés.

        Trois formes sont acceptées : le format courant (`{"processed": {...}}`),
        une simple liste d'identifiants, et un dictionnaire brut. Cette tolérance
        évite de perdre l'historique si le format évolue.

        Args:
            payload: Contenu JSON désérialisé.

        Returns:
            L'index clé -> métadonnées.
        """
        if isinstance(payload, dict) and "processed" in payload:
            processed = payload["processed"]
            if isinstance(processed, dict):
                return {str(k): (v if isinstance(v, dict) else {}) for k, v in processed.items()}
            if isinstance(processed, list):
                return {str(item): {} for item in processed}
            return {}
        if isinstance(payload, list):
            return {str(item): {} for item in payload}
        if isinstance(payload, dict):
            return {str(k): (v if isinstance(v, dict) else {}) for k, v in payload.items()}
        return {}

    def contains(self, trip: VelibTrip) -> bool:
        """Indique si le trajet a déjà été synchronisé.

        Args:
            trip: Trajet à tester.

        Returns:
            True si l'identifiant ou la clé temporelle est déjà enregistré.
        """
        return trip.trip_id in self._entries or time_key(trip.start_time) in self._entries

    def mark_processed(
        self, trip: VelibTrip, activity_id: int | str | None = None, note: str = ""
    ) -> None:
        """Enregistre un trajet comme synchronisé.

        Args:
            trip: Trajet traité.
            activity_id: Identifiant de l'activité Strava créée, si connu.
            note: Précision libre (« doublon », « repli en ligne droite »…).
        """
        metadata: dict[str, Any] = {
            "start_time": time_key(trip.start_time),
            "duration_seconds": trip.duration_seconds,
            "synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if activity_id is not None:
            metadata["strava_activity_id"] = str(activity_id)
        if note:
            metadata["note"] = note

        self._entries[trip.trip_id] = metadata
        # Clé de repli : indexée aussi, mais sans métadonnées dupliquées.
        self._entries.setdefault(time_key(trip.start_time), {"alias_of": trip.trip_id})
        self._dirty = True

    def save(self, force: bool = False) -> bool:
        """Écrit l'état sur disque.

        L'écriture passe par un fichier temporaire puis un remplacement atomique :
        une interruption au mauvais moment ne peut pas laisser un fichier d'état
        tronqué, ce qui ferait renvoyer tout l'historique à l'exécution suivante.

        Args:
            force: Écrire même en l'absence de modification.

        Returns:
            True si le fichier a été écrit.

        Raises:
            OSError: Si l'écriture échoue.
        """
        if not self._dirty and not force:
            logger.debug("État inchangé : pas d'écriture.")
            return False

        payload = {
            "version": STATE_VERSION,
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "processed": self._entries,
        }

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

        self._dirty = False
        logger.info("État écrit dans %s (%d entrées).", self.path, len(self._entries))
        return True
