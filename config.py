#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Chargement et validation de la configuration.

Toute la configuration transite par des variables d'environnement. En local,
`python-dotenv` les charge depuis un fichier `.env` ; en CI, GitHub Actions les
injecte depuis les secrets du dépôt. Aucun secret n'est écrit en dur.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent

DEFAULT_REPO_URL = "https://github.com/VOTRE_PSEUDO/velibsurstrava"
DEFAULT_STATE_FILE = PROJECT_ROOT / "processed_trips.json"
DEFAULT_GPX_FILE = PROJECT_ROOT / "temp_trip.gpx"


class ConfigError(RuntimeError):
    """Une variable d'environnement obligatoire est absente ou invalide."""


def _get_int(name: str, default: int) -> int:
    """Lit une variable entière, en retombant sur `default` si elle est illisible."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} doit être un entier, reçu : {raw!r}") from exc


def _get_bool(name: str, default: bool = False) -> bool:
    """Lit une variable booléenne (« 1 », « true », « yes », « oui »)."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "oui", "on"}


@dataclass(frozen=True)
class Config:
    """Configuration complète et validée d'une exécution.

    Attributes:
        velib_username: Adresse e-mail du compte Vélib'.
        velib_password: Mot de passe du compte Vélib'.
        strava_client_id: Identifiant de l'application Strava.
        strava_client_secret: Secret de l'application Strava.
        strava_refresh_token: Jeton de rafraîchissement OAuth2 (scope activity:write).
        ors_api_key: Clé de l'API OpenRouteService.
        repo_url: URL du dépôt, citée dans la description des activités.
        state_file: Chemin du fichier d'état des trajets déjà traités.
        gpx_file: Chemin du fichier GPX temporaire.
        max_trips_per_run: Nombre maximum de trajets traités par exécution.
        max_trip_age_days: Ancienneté maximale d'un trajet traité (0 = illimité).
        dry_run: Si True, aucun envoi n'est effectué vers Strava.
    """

    velib_username: str
    velib_password: str
    strava_client_id: str
    strava_client_secret: str
    strava_refresh_token: str
    ors_api_key: str
    repo_url: str = DEFAULT_REPO_URL
    state_file: Path = DEFAULT_STATE_FILE
    gpx_file: Path = DEFAULT_GPX_FILE
    max_trips_per_run: int = 25
    max_trip_age_days: int = 30
    dry_run: bool = False


def load_config(dotenv_path: Path | str | None = None) -> Config:
    """Charge la configuration depuis l'environnement (et le `.env` en local).

    Args:
        dotenv_path: Chemin explicite d'un fichier `.env`. Par défaut, le
            fichier `.env` situé à la racine du projet, s'il existe.

    Returns:
        Une instance `Config` validée.

    Raises:
        ConfigError: Si une variable obligatoire manque ou est invalide.
    """
    load_dotenv(dotenv_path or PROJECT_ROOT / ".env", override=False)

    # Le fichier .env.example initial employait VELIB_EMAIL ; les instructions et
    # le workflow CI imposent VELIB_USERNAME. On accepte les deux, la première
    # forme servant de repli, pour ne casser aucun .env déjà rempli.
    username = os.environ.get("VELIB_USERNAME") or os.environ.get("VELIB_EMAIL") or ""

    values = {
        "velib_username": username.strip(),
        "velib_password": (os.environ.get("VELIB_PASSWORD") or "").strip(),
        "strava_client_id": (os.environ.get("STRAVA_CLIENT_ID") or "").strip(),
        "strava_client_secret": (os.environ.get("STRAVA_CLIENT_SECRET") or "").strip(),
        "strava_refresh_token": (os.environ.get("STRAVA_REFRESH_TOKEN") or "").strip(),
        "ors_api_key": (os.environ.get("ORS_API_KEY") or "").strip(),
    }

    missing = [key.upper() for key, value in values.items() if not value]
    if missing:
        # On remet VELIB_USERNAME sous son vrai nom dans le message d'erreur.
        raise ConfigError(
            "Variables d'environnement manquantes ou vides : "
            + ", ".join(sorted(missing))
            + ". Copier .env.example en .env et renseigner les valeurs."
        )

    repo_url = (os.environ.get("PROJECT_REPO_URL") or DEFAULT_REPO_URL).strip()

    return Config(
        **values,
        repo_url=repo_url,
        state_file=Path(os.environ.get("STATE_FILE") or DEFAULT_STATE_FILE),
        gpx_file=Path(os.environ.get("GPX_FILE") or DEFAULT_GPX_FILE),
        max_trips_per_run=_get_int("MAX_TRIPS_PER_RUN", 25),
        max_trip_age_days=_get_int("MAX_TRIP_AGE_DAYS", 30),
        dry_run=_get_bool("DRY_RUN", False),
    )
