#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Récupération et normalisation de l'historique de trajets Vélib' Métropole.

La logique d'authentification reprend celle de `velib_history.py` (issue de
l'analyse d'un fichier HAR) :

  1. GET  /login                      -> jeton CSRF Symfony
  2. POST /login                      -> authentification, 302 vers /private/account
  3. GET  /api/private/getCourseList  -> historique JSON, paginé

L'authentification repose uniquement sur le cookie de session posé par le
serveur ; `requests.Session` le gère automatiquement. Aucun jeton Bearer.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable

import requests

from models import VelibTrip

logger = logging.getLogger(__name__)

BASE_URL = "https://www.velib-metropole.fr"
LOGIN_URL = f"{BASE_URL}/login"
ACCOUNT_URL = f"{BASE_URL}/private/account"
COURSE_LIST_URL = f"{BASE_URL}/api/private/getCourseList"

# En-têtes calqués sur ceux observés dans le HAR. Le User-Agent doit rester
# cohérent et récent : Cloudflare pénalise les UA par défaut de type
# « python-requests/2.x ».
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Ch-Ua": '"Chromium";v="152", "Not?A_Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}

PAGE_SIZE = 50           # le site utilise 10, l'API accepte davantage
REQUEST_TIMEOUT = 30     # secondes
MAX_RETRIES = 3
RETRY_BACKOFF = 3        # secondes, multiplié par le numéro de tentative
PAGE_DELAY = 0.5         # pause entre deux pages, par courtoisie
WARMUP_DELAY = 1.0       # pause après la visite d'amorçage, pour rester crédible

# L'API n'expose pas un schéma stable et documenté. Plutôt que de figer un seul
# nom de champ, on essaie une liste de candidats par ordre de préférence. Le
# premier champ présent et non vide l'emporte.
# Schéma confirmé sur la capture HAR du 5 septembre 2026 : les champs de trajet
# proprement dits vivent dans le sous-objet `parameter3`, tandis que la racine
# de l'enregistrement porte des données de facturation. `_first_present` cherche
# donc dans les deux, `parameter3` prioritaire.
_PARAMETER3_KEY = "parameter3"

_ID_KEYS = ("id", "courseId", "usageId", "operationId", "walletOperationId")
_DATE_KEYS = ("startDate", "operationDate", "date", "departureDate", "creationDate")
_END_DATE_KEYS = ("endDate", "arrivalDate", "stopDate")
# `quantity` est en SECONDES : l'API le déclare elle-même via
# `ratingUnitDescription: "second"`, et la valeur coïncide avec endDate-startDate
# à quelques secondes près sur les dix trajets de la capture.
_DURATION_KEYS = ("quantity", "duration", "durationInSeconds", "courseDuration")
_DEPARTURE_ID_KEYS = ("departureStationId", "departureStationCode", "startStationId")
_ARRIVAL_ID_KEYS = ("arrivalStationId", "arrivalStationCode", "endStationId")
_DEPARTURE_NAME_KEYS = ("departureStationName", "departureStation", "startStationName")
_ARRIVAL_NAME_KEYS = ("arrivalStationName", "arrivalStation", "endStationName")
_STATUS_KEYS = ("status", "courseStatus", "operationStatus", "state")
_BIKE_TYPE_KEYS = ("bikeType", "typeVelo", "vehicleType", "bikeElectric")
_DISTANCE_KEYS = ("DISTANCE", "distance", "distanceInMeters", "courseDistance")
_SPEED_KEYS = ("AVERAGE_SPEED", "averageSpeed")
_BIKE_ID_KEYS = ("BIKEID", "bikeId", "bikeNumber")

# Statuts considérés comme un trajet réellement effectué. La comparaison est
# insensible à la casse ; un statut absent est traité comme valide, l'API ne le
# renseignant pas systématiquement.
# « TREATED » est le statut réellement observé sur les trajets facturés ; c'est
# aussi le filtre appliqué côté serveur (paging.filters.status).
VALID_STATUSES = {
    "treated", "ok", "valid", "success", "closed", "finished", "done", "terminated",
}
INVALID_STATUSES = {"cancelled", "canceled", "annule", "annulé", "refused", "error", "failed"}


class VelibError(RuntimeError):
    """Erreur fonctionnelle du module (connexion, API, anti-bot)."""


class CloudflareChallenge(VelibError):
    """Cloudflare a interposé un challenge : l'IP appelante est filtrée."""


# --------------------------------------------------------------------------- #
# Utilitaires bas niveau
# --------------------------------------------------------------------------- #

# Deux familles de blocage sont rencontrées : le challenge JavaScript, et le
# refus sec par réputation d'IP (« Site not reachable »), qu'aucun réessai ne
# lèvera. Les deux se présentent en HTTP 403.
CHALLENGE_MARKERS = ("just a moment", "cf-chl", "challenge-platform", "cf_chl_opt")
IP_BLOCK_MARKERS = ("site not reachable", "access denied", "sorry, you have been blocked")


def _detect_cloudflare_block(response: requests.Response) -> None:
    """Lève `CloudflareChallenge` si la réponse est un blocage et non la page.

    Args:
        response: Réponse HTTP à inspecter.

    Raises:
        CloudflareChallenge: Si un challenge ou un blocage par IP est détecté.
    """
    mitigated = response.headers.get("cf-mitigated", "").lower()
    if mitigated != "challenge" and response.status_code not in (403, 503):
        return

    # `response.text` peut être illisible si le corps est compressé dans un
    # format que requests ne sait pas décoder ; on ne veut pas planter ici.
    try:
        body = response.text[:4000].lower()
    except Exception:  # pragma: no cover - dépend de l'encodage reçu
        body = ""

    if any(marker in body for marker in IP_BLOCK_MARKERS):
        raise CloudflareChallenge(
            f"Accès refusé par Cloudflare sur {response.url} (HTTP "
            f"{response.status_code}, page « site not reachable »). L'adresse IP "
            "appelante est filtrée par réputation. Les runners GitHub Actions "
            "hébergés partagent des plages Azure fréquemment bloquées : prévoir "
            "un runner auto-hébergé sur une connexion résidentielle."
        )

    if mitigated == "challenge" or any(marker in body for marker in CHALLENGE_MARKERS):
        raise CloudflareChallenge(
            f"Challenge Cloudflare détecté sur {response.url} "
            f"(HTTP {response.status_code}). L'IP appelante est probablement "
            "classée comme centre de données."
        )

    if response.status_code == 403:
        raise CloudflareChallenge(
            f"HTTP 403 sur {response.url} sans marqueur identifiable. Vérifier "
            "que le paquet Brotli est installé : sans lui, le corps de la "
            "réponse est illisible et le diagnostic devient impossible."
        )


def _request(
    session: requests.Session, method: str, url: str, **kwargs: Any
) -> requests.Response:
    """Exécute une requête avec réessais sur erreurs réseau et 5xx transitoires.

    Args:
        session: Session HTTP porteuse des cookies.
        method: Verbe HTTP.
        url: URL cible.
        **kwargs: Arguments transmis à `requests.Session.request`.

    Returns:
        La réponse HTTP obtenue.

    Raises:
        VelibError: Si toutes les tentatives échouent.
        CloudflareChallenge: Si un challenge anti-bot est détecté.
    """
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
            logger.warning(
                "%s %s : %s (tentative %d/%d)", method, url, exc, attempt, MAX_RETRIES
            )
        else:
            _detect_cloudflare_block(response)
            if response.status_code >= 500:
                last_error = VelibError(f"HTTP {response.status_code} sur {url}")
                logger.warning(
                    "HTTP %d sur %s (tentative %d/%d)",
                    response.status_code, url, attempt, MAX_RETRIES,
                )
            else:
                return response
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)
    raise VelibError(f"Échec définitif de {method} {url} : {last_error}")


def _extract_csrf_token(page_html: str) -> str:
    """Extrait la valeur du champ caché `_csrf_token` du formulaire de connexion.

    Args:
        page_html: Code HTML de la page `/login`.

    Returns:
        La valeur du jeton CSRF.

    Raises:
        VelibError: Si le champ est introuvable ou dépourvu de valeur.
    """
    field = re.search(
        r"""<input[^>]*name=["']_csrf_token["'][^>]*>""", page_html, re.IGNORECASE
    )
    if not field:
        raise VelibError(
            "Champ _csrf_token introuvable sur /login. Le formulaire a pu changer, "
            "ou la page renvoyée n'est pas la page de connexion."
        )
    value = re.search(r"""value=["']([^"']+)["']""", field.group(0))
    if not value:
        raise VelibError("Champ _csrf_token présent mais sans attribut value.")
    return value.group(1)


def _warn_if_captcha(page_html: str) -> None:
    """Lève une erreur explicite si un reCAPTCHA est présent sur `/login`.

    Le widget n'apparaît qu'après plusieurs échecs de connexion : le signaler
    tôt évite de chercher la panne ailleurs.

    Args:
        page_html: Code HTML de la page `/login`.

    Raises:
        VelibError: Si un widget reCAPTCHA est détecté.
    """
    if re.search(r"g-recaptcha|grecaptcha|recaptcha-login", page_html, re.IGNORECASE):
        raise VelibError(
            "Un widget reCAPTCHA est présent sur la page de connexion. Le compte est "
            "probablement en compteur d'échecs élevé : se reconnecter manuellement "
            "dans un navigateur pour remettre le compteur à zéro."
        )


# --------------------------------------------------------------------------- #
# Normalisation des enregistrements
# --------------------------------------------------------------------------- #

def _extract_parameter3(record: dict[str, Any]) -> dict[str, Any]:
    """Retourne le sous-objet `parameter3`, qui porte les données du trajet.

    Le champ est un objet JSON dans la capture observée, mais certaines API
    Symfony le sérialisent en chaîne : les deux formes sont acceptées.

    Args:
        record: Enregistrement brut de `walletOperations`.

    Returns:
        Le contenu de `parameter3`, ou un dictionnaire vide s'il est absent
        ou illisible.
    """
    raw = record.get(_PARAMETER3_KEY)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _first_present(record: dict[str, Any], keys: Iterable[str]) -> Any:
    """Renvoie la première valeur non vide parmi `keys`, sinon None.

    La recherche porte d'abord sur `parameter3` — où résident les données du
    trajet — puis sur la racine de l'enregistrement, qui porte la facturation.

    Args:
        record: Enregistrement brut.
        keys: Noms de champs candidats, par ordre de préférence.

    Returns:
        La première valeur non vide trouvée, ou None.
    """
    parameter3 = _extract_parameter3(record)
    for source in (parameter3, record):
        for key in keys:
            value = source.get(key)
            if value not in (None, "", [], {}):
                return value
    return None


def _parse_datetime(value: Any) -> datetime | None:
    """Convertit une date Vélib' en `datetime` conscient du fuseau, en UTC.

    Trois formes sont rencontrées : un horodatage epoch (secondes ou
    millisecondes), une chaîne ISO 8601, ou une chaîne « JJ/MM/AAAA HH:MM ».

    Args:
        value: Valeur brute issue du JSON.

    Returns:
        Le `datetime` en UTC, ou None si la valeur est inexploitable.
    """
    if value is None:
        return None

    # Horodatage epoch : l'API mélange secondes et millisecondes selon les champs.
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.isdigit()
    ):
        number = float(value)
        if number > 1e11:  # au-delà de l'an 5138 en secondes : ce sont des ms
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    # ISO 8601. `fromisoformat` n'accepte pas le « Z » avant Python 3.11 et gère
    # mal « +0200 » sans deux-points : on normalise d'abord.
    candidate = text.replace("Z", "+00:00")
    candidate = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", candidate)
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        parsed = None

    if parsed is None:
        for pattern in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue

    if parsed is None:
        return None

    # Une date sans fuseau vient de l'interface française : elle est en heure de
    # Paris. On la lit comme telle avant de repasser en UTC.
    if parsed.tzinfo is None:
        try:
            from zoneinfo import ZoneInfo

            parsed = parsed.replace(tzinfo=ZoneInfo("Europe/Paris"))
        except Exception:  # base de fuseaux absente du système
            parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_duration_seconds(value: Any) -> int | None:
    """Convertit une durée brute en secondes.

    Toutes les durées de l'API sont exprimées en secondes : `quantity` vaut
    1593 pour un trajet de 26 min 36 s, et l'enregistrement porte lui-même
    `ratingUnitDescription: "second"`.

    Args:
        value: Valeur brute (nombre, ou chaîne « MM:SS » / « HH:MM:SS »).

    Returns:
        La durée en secondes, ou None si inexploitable.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if ":" in text:
            parts = text.split(":")
            try:
                numbers = [int(part) for part in parts]
            except ValueError:
                return None
            seconds = 0
            for number in numbers:
                seconds = seconds * 60 + number
            return seconds
        try:
            return int(float(text))
        except ValueError:
            return None
    return None


def _is_status_valid(record: dict[str, Any]) -> bool:
    """Indique si le statut de l'enregistrement correspond à un trajet effectué."""
    status = _first_present(record, _STATUS_KEYS)
    if status is None:
        return True  # l'API ne renseigne pas toujours ce champ
    if isinstance(status, bool):
        return status
    text = str(status).strip().lower()
    if text in INVALID_STATUSES:
        return False
    if text in VALID_STATUSES:
        return True
    # Statut inconnu : on ne rejette pas, mais on le signale pour affiner la liste.
    logger.debug("Statut de trajet inconnu, conservé par défaut : %r", status)
    return True


def _normalise_bike_type(value: Any) -> str | None:
    """Normalise le type de vélo en « electrical » ou « mechanical »."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "electrical" if value else "mechanical"
    text = str(value).strip().lower()
    if not text:
        return None
    if any(marker in text for marker in ("elec", "élec", "ebike", "e-bike", "blue")):
        return "electrical"
    if any(marker in text for marker in ("mech", "méca", "meca", "green", "classic")):
        return "mechanical"
    return None


def _build_trip_id(record: dict[str, Any], start_time: datetime) -> str:
    """Construit une clé de déduplication stable pour un trajet.

    Args:
        record: Enregistrement brut.
        start_time: Horodatage de départ déjà normalisé.

    Returns:
        L'identifiant fourni par l'API, ou à défaut une clé dérivée de la date
        de départ et des stations — un même compte ne peut pas démarrer deux
        trajets à la même seconde.
    """
    native_id = _first_present(record, _ID_KEYS)
    if native_id is not None:
        return str(native_id)
    departure = _first_present(record, _DEPARTURE_ID_KEYS) or "?"
    arrival = _first_present(record, _ARRIVAL_ID_KEYS) or "?"
    return f"{start_time.strftime('%Y%m%dT%H%M%SZ')}-{departure}-{arrival}"


def normalise_trip(record: dict[str, Any]) -> VelibTrip | None:
    """Convertit un enregistrement brut de l'API en `VelibTrip`.

    Args:
        record: Un élément de `walletOperations`.

    Returns:
        Le trajet normalisé, ou None si l'enregistrement est inexploitable
        (date absente, durée nulle ou négative, statut invalide).
    """
    if not isinstance(record, dict):
        return None

    if not _is_status_valid(record):
        logger.debug("Trajet ignoré (statut invalide) : %s", record.get("status"))
        return None

    start_time = _parse_datetime(_first_present(record, _DATE_KEYS))
    if start_time is None:
        logger.debug("Trajet ignoré : aucune date de départ exploitable.")
        return None

    # La durée réelle est l'écart entre départ et arrivée. `quantity` la donne
    # aussi, en secondes, mais avec un écart systématique de 1 à 4 secondes
    # (arrondi de facturation) : on préfère donc le calcul quand `endDate` est
    # présent, et on retombe sur `quantity` sinon.
    duration_seconds: int | None = None
    end_time = _parse_datetime(_first_present(record, _END_DATE_KEYS))
    if end_time is not None:
        ecart = int((end_time - start_time).total_seconds())
        if ecart > 0:
            duration_seconds = ecart

    if duration_seconds is None:
        duration_seconds = _parse_duration_seconds(
            _first_present(record, _DURATION_KEYS)
        )

    if duration_seconds is None or duration_seconds <= 0:
        logger.debug(
            "Trajet ignoré : durée nulle ou absente (quantity=%r).",
            record.get("quantity"),
        )
        return None

    departure_id = _first_present(record, _DEPARTURE_ID_KEYS)
    arrival_id = _first_present(record, _ARRIVAL_ID_KEYS)

    # `DISTANCE` arrive en chaîne (« 3084.0 ») dans parameter3.
    distance_meters: float | None = None
    distance = _first_present(record, _DISTANCE_KEYS)
    if distance is not None:
        try:
            distance_meters = float(distance)
        except (TypeError, ValueError):
            distance_meters = None

    return VelibTrip(
        trip_id=_build_trip_id(record, start_time),
        start_time=start_time,
        duration_seconds=duration_seconds,
        departure_station_id=str(departure_id) if departure_id is not None else None,
        arrival_station_id=str(arrival_id) if arrival_id is not None else None,
        departure_station_name=_first_present(record, _DEPARTURE_NAME_KEYS),
        arrival_station_name=_first_present(record, _ARRIVAL_NAME_KEYS),
        bike_type=_normalise_bike_type(_first_present(record, _BIKE_TYPE_KEYS)),
        distance_meters=distance_meters,
        raw=record,
    )


# --------------------------------------------------------------------------- #
# Étapes fonctionnelles
# --------------------------------------------------------------------------- #

def build_session() -> requests.Session:
    """Crée une session HTTP portant les en-têtes d'un navigateur."""
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    return session


def login(session: requests.Session, username: str, password: str) -> None:
    """Authentifie la session ; les cookies sont conservés dans le bocal de session.

    Args:
        session: Session HTTP à authentifier.
        username: Adresse e-mail du compte.
        password: Mot de passe du compte.

    Raises:
        VelibError: Si les identifiants sont refusés ou le formulaire a changé.
        CloudflareChallenge: Si un challenge anti-bot bloque l'accès.
    """
    # 0. Amorçage : visiter l'accueil AVANT /login.
    #
    #    Cloudflare Bot Management refuse une requête vers /login qui arrive
    #    sans cookie `__cf_bm` — la réponse est alors un HTTP 403 portant la
    #    page « Site not reachable » de Vélib'. Un navigateur ne rencontre
    #    jamais ce cas puisqu'il atteint /login depuis une page du site. Cette
    #    visite préalable pose `__cf_bm` et rend /login accessible.
    #
    #    Mesuré : /login à froid → 403 systématique ; /login après une visite
    #    de l'accueil sur la même session → 200 systématique.
    home = _request(
        session, "GET", f"{BASE_URL}/",
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        },
    )
    if home.status_code != 200:
        logger.warning(
            "L'amorçage sur %s/ a renvoyé HTTP %d ; la suite peut échouer.",
            BASE_URL, home.status_code,
        )
    logger.debug("Cookies après amorçage : %s", sorted(session.cookies.keys()))
    time.sleep(WARMUP_DELAY)

    # 1. Page de connexion : fournit le jeton CSRF, lié au cookie de session.
    page = _request(
        session, "GET", LOGIN_URL,
        headers={
            "Referer": f"{BASE_URL}/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
        },
    )
    if page.status_code != 200:
        raise VelibError(f"GET /login a renvoyé HTTP {page.status_code}.")

    _warn_if_captcha(page.text)
    csrf_token = _extract_csrf_token(page.text)

    # 2. Soumission du formulaire. Les noms de champs viennent directement du HAR.
    payload = {
        "_username": username,
        "_password": password,
        "numFailures": "0",
        "_csrf_token": csrf_token,
        "redirectAfterLogin": "",
    }
    response = _request(
        session, "POST", LOGIN_URL,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": BASE_URL,
            "Referer": LOGIN_URL,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        allow_redirects=True,
    )

    # 3. Vérification : une connexion réussie redirige vers /private/account.
    #    Un échec réaffiche /login avec un message d'erreur (HTTP 200).
    if "/private/" not in response.url:
        raise VelibError(
            "Authentification refusée : la redirection attendue vers /private/account "
            f"n'a pas eu lieu (URL finale : {response.url}). Vérifier les identifiants."
        )
    logger.info("Connexion Vélib' réussie (redirection vers %s).", response.url)


def fetch_courses(
    session: requests.Session, page_size: int = PAGE_SIZE, max_pages: int = 100
) -> list[dict[str, Any]]:
    """Récupère la totalité de l'historique via la pagination offset/limit.

    Args:
        session: Session déjà authentifiée.
        page_size: Nombre d'enregistrements demandés par page.
        max_pages: Garde-fou contre une pagination qui ne se terminerait pas.

    Returns:
        La liste brute des enregistrements `walletOperations`.

    Raises:
        VelibError: Si la session a expiré ou si la réponse n'est pas du JSON.
    """
    api_headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": ACCOUNT_URL,
        "X-Requested-With": "XMLHttpRequest",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    trips: list[dict[str, Any]] = []
    offset = 0
    total: int | None = None

    for _ in range(max_pages):
        response = _request(
            session, "GET", COURSE_LIST_URL,
            params={"limit": page_size, "offset": offset},
            headers=api_headers,
        )

        if response.status_code in (301, 302, 401, 403):
            raise VelibError("Session expirée ou non authentifiée sur getCourseList.")
        if response.status_code != 200:
            raise VelibError(f"getCourseList a renvoyé HTTP {response.status_code}.")

        try:
            payload = response.json()
        except ValueError as exc:
            # Symptôme classique : on a reçu du HTML (page de login ou challenge).
            raise VelibError(
                f"Réponse non JSON sur getCourseList (offset={offset}) : {exc}"
            ) from exc

        status = payload.get("actionStatus", {}).get("status")
        if status and status != "SUCCESS":
            raise VelibError(f"L'API signale un statut d'erreur : {payload['actionStatus']}")

        batch = payload.get("walletOperations") or []
        trips.extend(batch)

        paging = payload.get("paging", {})
        total = paging.get("totalNumberOfRecords", total)
        logger.info(
            "getCourseList offset=%d : %d enregistrements (%d/%s).",
            offset, len(batch), len(trips), total if total is not None else "?",
        )

        if not batch:
            break
        if total is not None and len(trips) >= total:
            break

        offset += page_size
        time.sleep(PAGE_DELAY)
    else:
        logger.warning("Pagination interrompue après %d pages.", max_pages)

    return trips


def get_new_velib_trips(username: str, password: str) -> list[VelibTrip]:
    """Récupère l'historique Vélib' complet, normalisé et filtré.

    Seuls sont retournés les trajets au statut valide et de durée strictement
    positive. La déduplication vis-à-vis des exécutions précédentes relève de
    `state.py`, pas de ce module.

    Args:
        username: Adresse e-mail du compte Vélib'.
        password: Mot de passe du compte Vélib'.

    Returns:
        Les trajets exploitables, triés du plus ancien au plus récent.

    Raises:
        VelibError: En cas d'échec d'authentification ou de récupération.
    """
    session = build_session()
    try:
        login(session, username, password)
        records = fetch_courses(session)
    finally:
        session.close()

    if records:
        # Le schéma de l'API n'est pas documenté : journaliser les clés du premier
        # enregistrement permet d'ajuster les listes de candidats sans rejouer
        # toute la session.
        logger.debug("Clés du premier enregistrement : %s", sorted(records[0].keys()))

    trips = [trip for record in records if (trip := normalise_trip(record)) is not None]
    trips.sort(key=lambda trip: trip.start_time)
    logger.info("%d enregistrements bruts, %d trajets exploitables.", len(records), len(trips))
    return trips
