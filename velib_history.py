#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Récupération de l'historique de trajets Vélib' Métropole.

Stratégie retenue (voir le rapport d'analyse du HAR) :
  1. GET  /login                      -> récupère le jeton CSRF Symfony
  2. POST /login                      -> authentification, 302 vers /private/account
  3. GET  /api/private/getCourseList  -> historique JSON, paginé

L'authentification repose uniquement sur le cookie de session posé par le
serveur : requests.Session() le gère automatiquement. Aucun jeton Bearer.

Variables d'environnement attendues :
    VELIB_USERNAME  e-mail du compte
    VELIB_PASSWORD  mot de passe
    VELIB_OUTPUT    (optionnel) chemin du fichier JSON de sortie
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any

import requests

BASE_URL = "https://www.velib-metropole.fr"
LOGIN_URL = f"{BASE_URL}/login"
ACCOUNT_URL = f"{BASE_URL}/private/account"
COURSE_LIST_URL = f"{BASE_URL}/api/private/getCourseList"

# En-têtes calqués sur ceux observés dans le HAR. Le User-Agent doit rester
# cohérent et récent : Cloudflare pénalise les UA par défaut de type
# "python-requests/2.x".
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

PAGE_SIZE = 50          # le site utilise 10, l'API accepte davantage
REQUEST_TIMEOUT = 30     # secondes
MAX_RETRIES = 3
RETRY_BACKOFF = 3        # secondes, multiplié par le numéro de tentative


class VelibError(RuntimeError):
    """Erreur fonctionnelle du script (login, API, anti-bot)."""


class CloudflareChallenge(VelibError):
    """Cloudflare a interposé un challenge : bascule vers Playwright requise."""


# --------------------------------------------------------------------------- #
# Utilitaires bas niveau
# --------------------------------------------------------------------------- #

def _detect_cloudflare_block(response: requests.Response) -> None:
    """Lève CloudflareChallenge si la réponse est un challenge et non la page."""
    mitigated = response.headers.get("cf-mitigated", "").lower()
    if mitigated == "challenge" or response.status_code in (403, 503):
        body = response.text[:4000].lower()
        markers = ("just a moment", "cf-chl", "challenge-platform", "cf_chl_opt")
        if mitigated == "challenge" or any(m in body for m in markers):
            raise CloudflareChallenge(
                f"Challenge Cloudflare détecté sur {response.url} "
                f"(HTTP {response.status_code}). L'IP du runner est probablement "
                "classée comme datacenter : repasser sur la variante Playwright."
            )


def _request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """Requête avec réessais sur erreurs réseau et 5xx transitoires."""
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
            print(f"[warn] {method} {url} : {exc} (tentative {attempt}/{MAX_RETRIES})",
                  file=sys.stderr)
        else:
            _detect_cloudflare_block(response)
            if response.status_code >= 500:
                last_error = VelibError(f"HTTP {response.status_code} sur {url}")
                print(f"[warn] HTTP {response.status_code} sur {url} "
                      f"(tentative {attempt}/{MAX_RETRIES})", file=sys.stderr)
            else:
                return response
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)
    raise VelibError(f"Échec définitif de {method} {url} : {last_error}")


def _extract_csrf_token(page_html: str) -> str:
    """Extrait la valeur du champ caché _csrf_token du formulaire de connexion."""
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
    """Le reCAPTCHA n'apparaît qu'après plusieurs échecs : on prévient tôt."""
    if re.search(r"g-recaptcha|grecaptcha|recaptcha-login", page_html, re.IGNORECASE):
        raise VelibError(
            "Un widget reCAPTCHA est présent sur la page de connexion. Le compte est "
            "probablement en compteur d'échecs élevé : se reconnecter manuellement "
            "dans un navigateur pour remettre le compteur à zéro."
        )


# --------------------------------------------------------------------------- #
# Étapes fonctionnelles
# --------------------------------------------------------------------------- #

def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    return session


def login(session: requests.Session, username: str, password: str) -> None:
    """Authentifie la session (cookies stockés dans le session jar)."""
    # 1. Page de connexion : pose les cookies Cloudflare + session et fournit le CSRF.
    page = _request(
        session, "GET", LOGIN_URL,
        headers={"Referer": f"{BASE_URL}/",
                 "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
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
    print(f"[info] Connexion réussie, redirection vers {response.url}", file=sys.stderr)


def fetch_courses(session: requests.Session, page_size: int = PAGE_SIZE) -> list[dict[str, Any]]:
    """Récupère la totalité de l'historique via la pagination offset/limit."""
    api_headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": ACCOUNT_URL,
        "X-Requested-With": "XMLHttpRequest",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    trips: list[dict[str, Any]] = []
    offset = 0
    total = None

    while True:
        response = _request(
            session, "GET", COURSE_LIST_URL,
            params={"limit": page_size, "offset": offset},
            headers=api_headers,
        )

        if response.status_code == 401 or response.status_code == 302:
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
        print(f"[info] offset={offset} : {len(batch)} trajets "
              f"({len(trips)}/{total if total is not None else '?'})", file=sys.stderr)

        if not batch:
            break
        if total is not None and len(trips) >= total:
            break

        offset += page_size
        time.sleep(0.5)  # courtoisie vis-à-vis de l'origine

    return trips


# --------------------------------------------------------------------------- #
# Point d'entrée
# --------------------------------------------------------------------------- #

def main() -> int:
    username = os.environ.get("VELIB_USERNAME")
    password = os.environ.get("VELIB_PASSWORD")
    output_path = os.environ.get("VELIB_OUTPUT", "velib_history.json")

    if not username or not password:
        print("[erreur] Variables VELIB_USERNAME et VELIB_PASSWORD requises.",
              file=sys.stderr)
        return 2

    session = build_session()
    try:
        login(session, username, password)
        trips = fetch_courses(session)
    except CloudflareChallenge as exc:
        print(f"[erreur anti-bot] {exc}", file=sys.stderr)
        return 3
    except VelibError as exc:
        print(f"[erreur] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # filet de sécurité pour le run CI
        print(f"[erreur inattendue] {type(exc).__name__} : {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()

    try:
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(trips, handle, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(f"[erreur] Écriture de {output_path} impossible : {exc}", file=sys.stderr)
        return 1

    print(f"[ok] {len(trips)} trajets écrits dans {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())