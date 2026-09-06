#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Authentification OAuth2 et envoi d'activités vers Strava.

Le flux implémenté est celui du jeton de rafraîchissement : l'autorisation
interactive n'a lieu qu'une fois, à la main ; le script échange ensuite ce
`refresh_token` de longue durée contre un `access_token` valable six heures.

L'envoi d'un GPX est asynchrone côté Strava : `POST /uploads` retourne
immédiatement un identifiant d'envoi, qu'il faut interroger jusqu'à ce que
l'activité soit créée ou l'envoi rejeté.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://www.strava.com/api/v3"
TOKEN_URL = f"{API_BASE}/oauth/token"
UPLOADS_URL = f"{API_BASE}/uploads"

REQUEST_TIMEOUT = 60          # secondes
UPLOAD_POLL_INTERVAL = 3      # secondes entre deux vérifications d'état
UPLOAD_POLL_MAX_ATTEMPTS = 20  # soit une minute d'attente au maximum

#: Scope indispensable à la création d'activités. Strava le renvoie à chaque
#: rafraîchissement de jeton : le vérifier évite de découvrir son absence au
#: moment de l'envoi, après avoir consommé une requête OpenRouteService.
REQUIRED_SCOPE = "activity:write"

#: Scope facultatif permettant de relire une activité après son envoi. Sans lui,
#: le script sait que Strava a accepté le fichier, mais ne peut pas vérifier ce
#: que Strava en a fait. Deux variantes existent : `activity:read` ne donne accès
#: qu'aux activités visibles, `activity:read_all` y ajoute les activités privées.
READ_SCOPES = ("activity:read_all", "activity:read")

#: URL d'autorisation à ouvrir dans un navigateur pour réémettre le jeton.
AUTHORIZE_URL_TEMPLATE = (
    "https://www.strava.com/oauth/authorize"
    "?client_id={client_id}"
    "&response_type=code"
    "&redirect_uri=http://localhost/exchange_token"
    "&approval_prompt=force"
    "&scope=activity:write,activity:read_all"
)


class StravaError(RuntimeError):
    """Erreur d'authentification ou d'envoi vers Strava."""


class StravaDuplicateError(StravaError):
    """Strava a rejeté l'envoi car l'activité existe déjà.

    Ce n'est pas un échec : l'objectif — la présence de l'activité sur Strava —
    est atteint. L'appelant doit donc marquer le trajet comme traité.
    """


class StravaRateLimitError(StravaError):
    """Le quota d'appels de l'API Strava est épuisé (HTTP 429)."""


class StravaScopeError(StravaError):
    """Le jeton ne porte pas le scope `activity:write`, indispensable à l'envoi."""


def build_description(repo_url: str, extra: str | None = None) -> str:
    """Compose la description apposée à chaque activité créée.

    Args:
        repo_url: URL du dépôt du projet.
        extra: Ligne d'information complémentaire, facultative.

    Returns:
        La description au format texte.
    """
    lines = [
        "🚲 Trajet Vélib' synchronisé automatiquement.",
        f"Code source : {repo_url}",
    ]
    if extra:
        lines.insert(1, extra)
    return "\n".join(lines)


def parse_scopes(scope: str | None) -> set[str]:
    """Décompose la chaîne de scopes renvoyée par Strava.

    Args:
        scope: Valeur du champ `scope`, séparée par virgules ou espaces.

    Returns:
        L'ensemble des scopes accordés.
    """
    if not scope:
        return set()
    return {part.strip() for part in str(scope).replace(",", " ").split() if part.strip()}


def fetch_activity(access_token: str, activity_id: int | str) -> dict[str, Any] | None:
    """Relit une activité créée, pour vérifier ce que Strava en a fait.

    Args:
        access_token: Jeton d'accès valide.
        activity_id: Identifiant de l'activité.

    Returns:
        La description de l'activité, ou None si le jeton ne porte pas de scope
        de lecture — auquel cas Strava répond 404, sans distinguer ce cas d'une
        activité réellement absente.
    """
    try:
        response = requests.get(
            f"{API_BASE}/activities/{activity_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Relecture de l'activité %s impossible : %s", activity_id, exc)
        return None

    if response.status_code == 200:
        try:
            return response.json()
        except ValueError:
            return None

    if response.status_code in (401, 404):
        logger.debug(
            "Activité %s non relisible (HTTP %d) : le jeton ne porte "
            "probablement aucun scope de lecture.", activity_id, response.status_code,
        )
    return None


def get_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Échange le jeton de rafraîchissement contre un jeton d'accès frais.

    Args:
        client_id: Identifiant de l'application Strava.
        client_secret: Secret de l'application Strava.
        refresh_token: Jeton de rafraîchissement de longue durée.

    Returns:
        Le jeton d'accès, valable environ six heures.

    Raises:
        StravaError: Si Strava refuse l'échange ou renvoie une réponse invalide.
    """
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    try:
        response = requests.post(TOKEN_URL, data=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise StravaError(f"Appel du point d'accès OAuth2 impossible : {exc}") from exc

    if response.status_code != 200:
        raise StravaError(
            f"Rafraîchissement du jeton refusé (HTTP {response.status_code}) : "
            f"{response.text[:300]}. Vérifier STRAVA_CLIENT_ID, "
            "STRAVA_CLIENT_SECRET et STRAVA_REFRESH_TOKEN."
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise StravaError(f"Réponse OAuth2 non JSON : {exc}") from exc

    access_token = data.get("access_token")
    if not access_token:
        raise StravaError(f"Réponse OAuth2 sans access_token : {data}")

    # Strava fait tourner le refresh_token : si un nouveau est renvoyé, il faut
    # le reporter dans les secrets, faute de quoi l'ancien finira par expirer.
    new_refresh = data.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        logger.warning(
            "Strava a émis un nouveau refresh_token. Mettre à jour le secret "
            "STRAVA_REFRESH_TOKEN avec : %s", new_refresh
        )

    # Strava renvoie les scopes accordés dans la réponse de rafraîchissement.
    # Les vérifier ici épargne un envoi voué à un HTTP 401, et surtout donne un
    # message actionnable plutôt que « Authorization Error ».
    scope = str(data.get("scope") or "")
    granted = parse_scopes(scope)
    if granted and REQUIRED_SCOPE not in granted:
        raise StravaScopeError(
            f"Le jeton Strava porte les scopes {sorted(granted)} mais pas "
            f"« {REQUIRED_SCOPE} », indispensable pour créer une activité.\n"
            "Réémettre le jeton en ouvrant cette URL dans un navigateur, puis en "
            "échangeant le paramètre « code » de l'URL de retour :\n"
            + AUTHORIZE_URL_TEMPLATE.format(client_id=client_id)
        )

    if granted and not granted.intersection(READ_SCOPES):
        # Pas bloquant : l'envoi fonctionne. Mais sans relecture possible, la
        # seule preuve qu'une activité existe est la réponse de l'envoi.
        logger.info(
            "Le jeton ne porte aucun scope de lecture (%s) : les activités "
            "créées ne pourront pas être relues pour vérification. Pour "
            "l'activer, réémettre le jeton avec « activity:read_all » : %s",
            ", ".join(READ_SCOPES),
            AUTHORIZE_URL_TEMPLATE.format(client_id=client_id),
        )

    logger.info(
        "Jeton d'accès Strava obtenu (expire à %s, scopes : %s).",
        data.get("expires_at"), scope or "non communiqués",
    )
    return str(access_token)


def _check_rate_limit(response: requests.Response) -> None:
    """Convertit un HTTP 429 en exception dédiée.

    Raises:
        StravaRateLimitError: Si le quota est épuisé.
    """
    if response.status_code == 429:
        usage = response.headers.get("X-RateLimit-Usage", "inconnu")
        limit = response.headers.get("X-RateLimit-Limit", "inconnu")
        raise StravaRateLimitError(
            f"Quota Strava épuisé (usage {usage} pour une limite de {limit}). "
            "Réessayer après le prochain quart d'heure."
        )


def _poll_upload(access_token: str, upload_id: int | str) -> dict[str, Any]:
    """Interroge l'état d'un envoi jusqu'à son aboutissement.

    Args:
        access_token: Jeton d'accès valide.
        upload_id: Identifiant retourné par `POST /uploads`.

    Returns:
        La charge utile finale de l'envoi, contenant `activity_id`.

    Raises:
        StravaDuplicateError: Si l'activité existait déjà.
        StravaError: Si Strava rejette l'envoi ou si le délai est dépassé.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    status_url = f"{UPLOADS_URL}/{upload_id}"

    for attempt in range(1, UPLOAD_POLL_MAX_ATTEMPTS + 1):
        time.sleep(UPLOAD_POLL_INTERVAL)
        try:
            response = requests.get(status_url, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("Vérification de l'envoi %s impossible : %s", upload_id, exc)
            continue

        _check_rate_limit(response)
        if response.status_code != 200:
            logger.warning(
                "Vérification de l'envoi %s : HTTP %d.", upload_id, response.status_code
            )
            continue

        try:
            data = response.json()
        except ValueError:
            logger.warning("Réponse non JSON lors de la vérification de l'envoi.")
            continue

        error = data.get("error")
        if error:
            if "duplicate" in str(error).lower():
                raise StravaDuplicateError(f"Activité déjà présente sur Strava : {error}")
            raise StravaError(f"Strava a rejeté l'envoi {upload_id} : {error}")

        activity_id = data.get("activity_id")
        if activity_id:
            logger.info(
                "Envoi %s traité : activité %s créée.", upload_id, activity_id
            )
            return data

        logger.debug(
            "Envoi %s en cours de traitement (%s), tentative %d/%d.",
            upload_id, data.get("status"), attempt, UPLOAD_POLL_MAX_ATTEMPTS,
        )

    raise StravaError(
        f"L'envoi {upload_id} n'a pas abouti dans le délai imparti "
        f"({UPLOAD_POLL_INTERVAL * UPLOAD_POLL_MAX_ATTEMPTS} s). "
        "Il peut néanmoins se terminer côté Strava : vérifier avant de relancer."
    )


def upload_to_strava(
    access_token: str,
    gpx_file_path: Path | str,
    trip_name: str,
    description: str = "",
    activity_type: str = "ride",
    wait_for_completion: bool = True,
) -> dict[str, Any]:
    """Envoie un fichier GPX sur Strava et attend la création de l'activité.

    Args:
        access_token: Jeton d'accès valide (scope `activity:write`).
        gpx_file_path: Chemin du fichier GPX à envoyer.
        trip_name: Nom donné à l'activité.
        description: Description de l'activité.
        activity_type: Type d'activité Strava (« ride » pour un vélo).
        wait_for_completion: Si True, interroge Strava jusqu'à obtenir
            l'identifiant de l'activité créée.

    Returns:
        La charge utile de l'envoi. Contient `activity_id` si
        `wait_for_completion` est True.

    Raises:
        StravaDuplicateError: Si l'activité existe déjà sur Strava.
        StravaRateLimitError: Si le quota d'appels est épuisé.
        StravaError: Pour toute autre erreur d'envoi.
    """
    gpx_path = Path(gpx_file_path)
    if not gpx_path.is_file():
        raise StravaError(f"Fichier GPX introuvable : {gpx_path}")

    headers = {"Authorization": f"Bearer {access_token}"}
    data = {
        "data_type": "gpx",
        "activity_type": activity_type,
        "name": trip_name,
        "description": description,
    }

    try:
        with gpx_path.open("rb") as handle:
            files = {"file": (gpx_path.name, handle, "application/gpx+xml")}
            response = requests.post(
                UPLOADS_URL,
                headers=headers,
                data=data,
                files=files,
                timeout=REQUEST_TIMEOUT,
            )
    except requests.RequestException as exc:
        raise StravaError(f"Envoi vers Strava impossible : {exc}") from exc
    except OSError as exc:
        raise StravaError(f"Lecture de {gpx_path} impossible : {exc}") from exc

    _check_rate_limit(response)

    if response.status_code == 401:
        raise StravaError(
            "Jeton d'accès refusé (HTTP 401). Le refresh token doit avoir été "
            "émis avec le scope « activity:write »."
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise StravaError(
            f"Réponse d'envoi non JSON (HTTP {response.status_code}) : {exc}"
        ) from exc

    if response.status_code >= 400:
        message = payload.get("message") or payload.get("error") or response.text[:300]
        if "duplicate" in str(message).lower():
            raise StravaDuplicateError(f"Activité déjà présente sur Strava : {message}")
        raise StravaError(f"Envoi refusé (HTTP {response.status_code}) : {message}")

    error = payload.get("error")
    if error:
        if "duplicate" in str(error).lower():
            raise StravaDuplicateError(f"Activité déjà présente sur Strava : {error}")
        raise StravaError(f"Strava a rejeté l'envoi : {error}")

    upload_id = payload.get("id") or payload.get("id_str")
    if not upload_id:
        raise StravaError(f"Réponse d'envoi sans identifiant : {payload}")

    logger.info("GPX transmis à Strava (envoi n° %s).", upload_id)

    if not wait_for_completion:
        return payload
    return _poll_upload(access_token, upload_id)
