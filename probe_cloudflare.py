#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sonde le pare-feu Cloudflare de velib-metropole.fr, sans identifiants.

Ce script répond à une seule question : *depuis cette machine, avec ce moteur
HTTP, le site répond-il ?* Il n'envoie aucun identifiant et ne se connecte à
rien — il s'arrête à `GET /login`, la page de formulaire, qui est publique.

Il est conçu pour tourner sur un runner GitHub Actions après un échec, là où
l'on ne peut ni rejouer une requête à la main ni attacher un débogueur. Quatre
mesures en sortent :

1. `GET /cdn-cgi/trace` — l'IP publique vue par Cloudflare et le datacentre qui
   a servi la requête. Ce point de terminaison n'est jamais filtré : s'il
   échoue, le problème est réseau, pas Cloudflare.
2. `GET /login` sur une session neuve — le cas « à froid », sans cookie
   `__cf_bm`. Historiquement un 403 systématique.
3. `GET /` puis `GET /login` sur la même session — la séquence qu'emploie
   `velib.login()`.
4. Le tout répété pour chaque moteur HTTP disponible, ce qui isole la part de
   l'empreinte TLS/HTTP dans le refus : si `curl` passe là où `requests`
   échoue, l'empreinte était le signal déterminant ; si les deux échouent
   identiquement, c'est l'adresse IP.

Usage :

    python probe_cloudflare.py                 # tous les moteurs disponibles
    python probe_cloudflare.py --backend curl  # un seul
    python probe_cloudflare.py --verbose       # vide aussi les réponses réussies

Code de sortie : 0 si au moins un moteur atteint `/login`, 3 sinon.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any

import http_client

logger = logging.getLogger("probe")

BASE_URL = "https://www.velib-metropole.fr"
TRACE_URL = f"{BASE_URL}/cdn-cgi/trace"
LOGIN_URL = f"{BASE_URL}/login"

TIMEOUT = 30
WARMUP_DELAY = 1.0

# En-têtes d'une navigation réelle vers une page. Le moteur `curl` les pose déjà
# via son empreinte, mais les redire ici garantit que les deux moteurs sont
# comparés à en-têtes égaux — sans quoi la sonde mesurerait deux variables à la
# fois et ne prouverait rien.
NAVIGATION_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


def _tenter(
    session: http_client.HttpSession,
    url: str,
    etape: str,
    verbose: bool,
    headers: dict[str, str] | None = None,
) -> Any | None:
    """Exécute une requête et journalise ce qu'elle révèle.

    Args:
        session: Session à employer.
        url: URL à demander.
        etape: Libellé de l'étape, repris dans les journaux.
        verbose: Si True, vide aussi les réponses réussies.
        headers: En-têtes additionnels.

    Returns:
        La réponse, ou None si la requête n'a pas abouti.
    """
    try:
        response = session.request(
            "GET", url, timeout=TIMEOUT, headers=headers or NAVIGATION_HEADERS
        )
    except http_client.network_errors() as exc:
        logger.error("%s : échec réseau — %s : %s", etape, type(exc).__name__, exc)
        return None

    reussi = response.status_code == 200
    logger.info(
        "%s : HTTP %d%s", etape, response.status_code, "" if reussi else "  <-- ANORMAL"
    )
    if not reussi or verbose:
        http_client.dump_response(
            response,
            label=etape,
            session=session,
            level=logging.INFO if reussi else logging.ERROR,
        )
    return response


def sonder_moteur(backend: str, verbose: bool) -> bool:
    """Exécute la batterie de mesures pour un moteur HTTP donné.

    Args:
        backend: `curl` ou `requests`.
        verbose: Si True, vide aussi les réponses réussies.

    Returns:
        True si `/login` a été atteint en HTTP 200 après amorçage.
    """
    logger.info("")
    logger.info("#" * 78)
    logger.info("# MOTEUR : %s", backend)
    logger.info("#" * 78)

    # 1. À froid : /login sans cookie __cf_bm, sur une session neuve.
    froide = http_client.build_session(backend)
    try:
        a_froid = _tenter(froide, LOGIN_URL, f"[{backend}] GET /login à froid", verbose)
    finally:
        froide.close()

    # 2. Séquence réelle : accueil puis /login, même session.
    session = http_client.build_session(backend)
    try:
        _tenter(session, TRACE_URL, f"[{backend}] GET /cdn-cgi/trace", verbose=True,
                headers={"Accept": "text/plain"})
        _tenter(session, f"{BASE_URL}/", f"[{backend}] GET / (amorçage)", verbose)
        logger.info(
            "[%s] cookies après amorçage : %s",
            backend, ", ".join(http_client.cookie_names(session)) or "aucun",
        )
        time.sleep(WARMUP_DELAY)
        apres = _tenter(
            session, LOGIN_URL, f"[{backend}] GET /login après amorçage", verbose,
            headers={**NAVIGATION_HEADERS, "Referer": f"{BASE_URL}/",
                     "Sec-Fetch-Site": "same-origin"},
        )
    finally:
        session.close()

    froid_ok = a_froid is not None and a_froid.status_code == 200
    apres_ok = apres is not None and apres.status_code == 200

    logger.info("")
    logger.info("[%s] BILAN : /login à froid %s, /login après amorçage %s",
                backend, "OK" if froid_ok else "REFUSÉ", "OK" if apres_ok else "REFUSÉ")
    if apres_ok and not froid_ok:
        logger.info(
            "[%s] Le cookie __cf_bm est bien le facteur déterminant : l'amorçage "
            "sur l'accueil est indispensable.", backend,
        )
    return apres_ok


def main(argv: list[str] | None = None) -> int:
    """Point d'entrée de la sonde.

    Returns:
        0 si au moins un moteur atteint `/login`, 3 sinon.
    """
    parser = argparse.ArgumentParser(
        description="Sonde le pare-feu Cloudflare de velib-metropole.fr "
                    "(aucun identifiant requis).",
    )
    parser.add_argument(
        "--backend", choices=["curl", "requests", "all"], default="all",
        help="Moteur HTTP à sonder. « all » (défaut) compare tous ceux qui sont "
             "installés : c'est cette comparaison qui isole la part de "
             "l'empreinte TLS dans un refus.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Vide aussi les en-têtes et le corps des réponses réussies.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(message)s", stream=sys.stdout,
    )

    if args.backend == "all":
        moteurs = [http_client.BACKEND_REQUESTS]
        if http_client.curl_available():
            moteurs.append(http_client.BACKEND_CURL)
        else:
            logger.warning(
                "curl_cffi n'est pas installé : la comparaison la plus utile — "
                "empreinte Python contre empreinte Chrome — ne peut pas être "
                "faite. Lancer « pip install curl_cffi »."
            )
    else:
        moteurs = [args.backend]

    logger.info("Sonde Cloudflare — %s", BASE_URL)
    logger.info("curl_cffi : %s", http_client.curl_version() or "absent")
    logger.info("Cible d'imitation demandée : %s", http_client.impersonate_target())

    resultats = {moteur: sonder_moteur(moteur, args.verbose) for moteur in moteurs}

    logger.info("")
    logger.info("=" * 78)
    logger.info("SYNTHÈSE")
    logger.info("=" * 78)
    for moteur, ok in resultats.items():
        logger.info("  %-10s /login : %s", moteur, "ATTEINT" if ok else "REFUSÉ")

    if resultats.get(http_client.BACKEND_CURL) and not resultats.get(
        http_client.BACKEND_REQUESTS, True
    ):
        logger.info("")
        logger.info(
            "Conclusion : l'empreinte TLS/HTTP était bien le signal bloquant. "
            "Poser VELIB_HTTP_BACKEND=curl dans le workflow."
        )
    elif not any(resultats.values()):
        logger.error("")
        logger.error(
            "Conclusion : aucun moteur ne passe. L'empreinte n'est donc pas le "
            "seul facteur — relire les vidages ci-dessus : un code WAF 1020 "
            "désigne une règle de pare-feu (probablement sur l'ASN ou le pays), "
            "un code 1015 une limitation de débit. Dans le premier cas, la seule "
            "parade reste un runner auto-hébergé."
        )

    return 0 if any(resultats.values()) else 3


if __name__ == "__main__":
    sys.exit(main())
