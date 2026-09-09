#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Transport HTTP et diagnostic des réponses de velib-metropole.fr.

Deux moteurs sont interchangeables derrière la même interface, celle de
`requests.Session` :

* `requests` — la pile Python standard. Empreinte TLS et HTTP/1.1 propre à
  OpenSSL/urllib3 : reconnaissable au premier coup d'œil pour Cloudflare.
* `curl` — `curl_cffi`, qui embarque *curl-impersonate* et reproduit l'empreinte
  TLS (ordre des ciphers, extensions, courbes, JA3/JA4) et HTTP/2 (trame
  SETTINGS, ordre des pseudo-en-têtes) d'un Chrome réel.

Le choix se fait par la variable d'environnement `VELIB_HTTP_BACKEND` :
`auto` (défaut, `curl` s'il est installé), `curl`, ou `requests`.

Pourquoi cela compte : Cloudflare Bot Management additionne des signaux pour
calculer un *Threat Score*. La réputation de l'IP en est un — les plages Azure
des runners GitHub Actions hébergés sont médiocrement notées — et l'empreinte
TLS/HTTP en est un autre. Une IP de centre de données seule peut passer ; une
IP de centre de données *plus* une empreinte « Python » ne passe pas. On ne
peut rien à l'IP, on peut tout à l'empreinte.

Ce module porte aussi `describe_response()`, le vidage de diagnostic qui rend
un blocage lisible dans les journaux d'un runner : en-têtes complets, code
d'erreur WAF, corps brut. Les valeurs des cookies et des en-têtes
d'authentification y sont masquées — les journaux d'un dépôt public sont
publics — et les corps de *requête* ne sont jamais journalisés, celui du POST
`/login` contenant le mot de passe.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterator, MutableMapping, Protocol

import requests

logger = logging.getLogger(__name__)

BACKEND_REQUESTS = "requests"
BACKEND_CURL = "curl"
BACKEND_AUTO = "auto"

#: Cible d'imitation par défaut. `curl_cffi` résout « chrome » vers la version
#: de Chrome la plus récente qu'il sache imiter, ce qui évite d'épingler un
#: numéro qui vieillirait mal. Surchargeable par `VELIB_IMPERSONATE`.
DEFAULT_IMPERSONATE = "chrome"

#: Nombre de caractères de corps de réponse journalisés lors d'un diagnostic.
#: Une page de blocage Cloudflare tient largement dans cette limite ; la page
#: d'accueil de Vélib', non, et il est inutile de la déverser en entier.
DUMP_BODY_CHARS = 4000

# En-têtes du moteur `requests` : celui-ci n'imite rien, il faut donc écrire à
# la main un jeu cohérent. Toute incohérence est un signal pour Cloudflare, d'où
# le trio User-Agent / Sec-Ch-Ua / Sec-Ch-Ua-Platform qui doit annoncer la même
# version et le même système.
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

# En-têtes ajoutés au moteur `curl`. La liste est volontairement réduite à un
# seul élément : `curl_cffi` pose déjà User-Agent, Sec-Ch-Ua*, Accept et
# Accept-Encoding conformes à la version de Chrome imitée, et ses valeurs par
# défaut ne remplacent JAMAIS celles que l'appelant fournit. Y réinjecter
# `BROWSER_HEADERS` produirait donc un User-Agent « Windows » sur une empreinte
# macOS — exactement le genre d'incohérence que Cloudflare sanctionne.
# Accept-Language est la seule exception : l'imitation annonce « en-US », et un
# navigateur qui consulte un service parisien annonce le français.
IMPERSONATED_HEADERS = {
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

# Codes d'erreur du WAF Cloudflare, tels qu'affichés dans la page de blocage.
# Ils disent *pourquoi* la requête a été refusée, ce qu'aucun code HTTP 403 ne
# dit : 1020 désigne une règle de pare-feu du client, 1010 l'empreinte du
# client, 1006-1008 un bannissement d'IP. Le diagnostic bascule d'une hypothèse
# à un fait dès qu'un de ces codes apparaît.
WAF_ERROR_CODES = {
    "1006": "IP bannie par le propriétaire du site (Access denied).",
    "1007": "IP bannie automatiquement pour comportement abusif.",
    "1008": "IP bannie par une règle de pare-feu du propriétaire.",
    "1009": "Pays bloqué par le propriétaire du site.",
    "1010": (
        "Empreinte du client refusée (« The owner of this website has banned "
        "your access based on your browser's signature »). C'EST le cas que "
        "curl-impersonate traite : l'empreinte TLS/HTTP a été reconnue comme "
        "non navigateur."
    ),
    "1012": "Accès refusé par une règle du propriétaire du site.",
    "1015": "Limitation de débit (rate limiting) : trop de requêtes.",
    "1020": (
        "Access denied : une règle de pare-feu (WAF / Custom Rule) a rejeté la "
        "requête. Le critère peut être l'ASN, le pays, le Threat Score, le "
        "User-Agent ou le chemin demandé."
    ),
    "1101": "Erreur d'exécution d'un Worker Cloudflare (côté site).",
    "1102": "Worker Cloudflare : limite de ressources atteinte (côté site).",
    "1200": "Page bloquée par Cloudflare Web Application Firewall.",
}

# En-têtes de réponse dont la seule présence oriente le diagnostic.
CLOUDFLARE_HEADERS = (
    "server", "cf-ray", "cf-mitigated", "cf-cache-status", "cf-chl-out",
    "cf-chl-bypass", "retry-after", "location", "content-type",
    "content-encoding", "x-generator",
)

# En-têtes dont la valeur est masquée dans les vidages : un journal de runner
# est archivé, et celui d'un dépôt public est lisible par tout le monde.
SENSITIVE_HEADERS = {"set-cookie", "authorization", "proxy-authorization", "cookie"}


class HttpSession(Protocol):
    """Surface commune de `requests.Session` et de `curl_cffi.requests.Session`."""

    headers: MutableMapping[str, str]

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        ...

    def close(self) -> None:
        ...


class BackendUnavailable(RuntimeError):
    """Le moteur HTTP explicitement demandé n'est pas installable."""


# --------------------------------------------------------------------------- #
# Choix et construction du moteur
# --------------------------------------------------------------------------- #

def _curl_module() -> Any | None:
    """Importe `curl_cffi.requests`, ou retourne None s'il est absent."""
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        return None
    return curl_requests


def curl_available() -> bool:
    """Indique si `curl_cffi` est installé et importable."""
    return _curl_module() is not None


def curl_version() -> str | None:
    """Retourne la version de `curl_cffi`, ou None s'il est absent."""
    try:
        import curl_cffi
    except ImportError:
        return None
    return getattr(curl_cffi, "__version__", "inconnue")


def resolve_backend(requested: str | None = None) -> str:
    """Détermine le moteur HTTP à employer.

    Args:
        requested: Valeur explicite (`auto`, `curl`, `requests`). Par défaut,
            la variable d'environnement `VELIB_HTTP_BACKEND`, elle-même
            défaillant sur `auto`.

    Returns:
        `BACKEND_CURL` ou `BACKEND_REQUESTS`.

    Raises:
        BackendUnavailable: Si `curl` est demandé explicitement mais absent.
            L'échec est immédiat et bruyant : un repli silencieux sur
            `requests` en CI redonnerait le HTTP 403 sans expliquer pourquoi.
    """
    choice = (requested or os.environ.get("VELIB_HTTP_BACKEND") or BACKEND_AUTO).strip().lower()

    if choice == BACKEND_CURL:
        if not curl_available():
            raise BackendUnavailable(
                "VELIB_HTTP_BACKEND=curl mais curl_cffi n'est pas installé. "
                "Lancer « pip install curl_cffi » (ou pip install -r "
                "requirements.txt)."
            )
        return BACKEND_CURL

    if choice == BACKEND_REQUESTS:
        return BACKEND_REQUESTS

    if choice != BACKEND_AUTO:
        logger.warning(
            "VELIB_HTTP_BACKEND=%r inconnu ; valeurs acceptées : auto, curl, "
            "requests. Repli sur auto.", choice,
        )

    return BACKEND_CURL if curl_available() else BACKEND_REQUESTS


def impersonate_target() -> str:
    """Retourne la cible d'imitation demandée (`VELIB_IMPERSONATE`)."""
    return (os.environ.get("VELIB_IMPERSONATE") or DEFAULT_IMPERSONATE).strip()


def build_session(backend: str | None = None) -> HttpSession:
    """Construit une session HTTP portant l'empreinte d'un navigateur.

    Args:
        backend: Moteur à forcer. Par défaut, `resolve_backend()` décide.

    Returns:
        Une session compatible `requests.Session`, dont l'attribut
        `velib_backend` indique le moteur retenu.

    Raises:
        BackendUnavailable: Si le moteur `curl` est exigé mais absent.
    """
    resolved = resolve_backend(backend)

    if resolved == BACKEND_CURL:
        curl_requests = _curl_module()
        assert curl_requests is not None  # garanti par resolve_backend
        target = impersonate_target()
        session = curl_requests.Session(impersonate=target)
        session.headers.update(IMPERSONATED_HEADERS)
        session.velib_backend = BACKEND_CURL
        session.velib_impersonate = target
        logger.info(
            "Moteur HTTP : curl_cffi %s, empreinte imitée « %s ».",
            curl_version(), target,
        )
        return session

    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    session.velib_backend = BACKEND_REQUESTS
    session.velib_impersonate = None
    logger.info(
        "Moteur HTTP : requests %s (aucune imitation d'empreinte TLS). "
        "Sur un runner hébergé, Cloudflare peut refuser cette empreinte : "
        "installer curl_cffi et poser VELIB_HTTP_BACKEND=curl.",
        requests.__version__,
    )
    return session


def describe_session(session: HttpSession) -> str:
    """Décrit le moteur d'une session, pour les messages de diagnostic."""
    backend = getattr(session, "velib_backend", "inconnu")
    target = getattr(session, "velib_impersonate", None)
    return f"{backend} (empreinte imitée : {target})" if target else str(backend)


def network_errors() -> tuple[type[BaseException], ...]:
    """Retourne les exceptions réseau à intercepter, tous moteurs confondus.

    `curl_cffi` n'hérite pas de `requests.RequestException` : sans cette
    agrégation, une coupure réseau sous le moteur `curl` remonterait telle
    quelle et court-circuiterait les réessais.
    """
    errors: list[type[BaseException]] = [requests.RequestException]
    curl_requests = _curl_module()
    if curl_requests is not None:
        for name in ("RequestException", "RequestsError"):
            candidate = getattr(curl_requests, name, None)
            if isinstance(candidate, type) and issubclass(candidate, BaseException):
                errors.append(candidate)
        exceptions = getattr(curl_requests, "exceptions", None)
        candidate = getattr(exceptions, "RequestException", None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            errors.append(candidate)
    return tuple(dict.fromkeys(errors))


def cookie_names(session: HttpSession) -> list[str]:
    """Liste les noms des cookies détenus par une session, valeurs exclues.

    Les deux moteurs exposent un bocal de cookies d'API différente ; l'échec
    d'énumération n'est jamais bloquant, il s'agit d'une aide au diagnostic.
    """
    jar = getattr(session, "cookies", None)
    if jar is None:
        return []
    try:
        return sorted(jar.keys())
    except Exception:  # pragma: no cover - dépend du moteur
        try:
            return sorted(cookie.name for cookie in jar)
        except Exception:
            return []


# --------------------------------------------------------------------------- #
# Diagnostic d'une réponse
# --------------------------------------------------------------------------- #

def _header_items(response: Any) -> Iterator[tuple[str, str]]:
    """Itère les en-têtes de réponse, un couple par occurrence.

    Les en-têtes répétables — `Set-Cookie` au premier chef — sont fusionnés par
    `requests` en une seule chaîne séparée par des virgules, ce qui rend un
    vidage illisible. On passe donc par les API multi-valeurs quand elles
    existent (`Headers.multi_items` chez `curl_cffi`, `HTTPHeaderDict` de
    `urllib3` chez `requests`).
    """
    headers = getattr(response, "headers", None)
    if headers is None:
        return

    multi = getattr(headers, "multi_items", None)
    if callable(multi):
        try:
            yield from multi()
            return
        except Exception:  # pragma: no cover - dépend du moteur
            pass

    raw = getattr(getattr(response, "raw", None), "headers", None)
    get_list = getattr(raw, "getlist", None)
    if callable(get_list):
        try:
            for key in dict.fromkeys(raw.keys()):
                for value in get_list(key):
                    yield key, value
            return
        except Exception:  # pragma: no cover - dépend de la version d'urllib3
            pass

    try:
        yield from headers.items()
    except Exception:  # pragma: no cover
        return


def _mask_header(name: str, value: str) -> str:
    """Masque la valeur d'un en-tête sensible en conservant sa structure.

    Un `Set-Cookie` reste informatif sans sa valeur : le nom du cookie dit s'il
    s'agit de `__cf_bm`, de `cf_clearance` ou de la session applicative, et les
    attributs (`Path`, `Secure`, `Max-Age`) sont conservés.
    """
    if name.lower() not in SENSITIVE_HEADERS:
        return value

    if name.lower() == "set-cookie" and "=" in value:
        cookie, _, remainder = value.partition("=")
        attributes = remainder.split(";", 1)
        suffix = f";{attributes[1]}" if len(attributes) > 1 else ""
        return f"{cookie}=<valeur masquée>{suffix}"
    return "<valeur masquée>"


def _decode_body(response: Any) -> str:
    """Décode le corps d'une réponse sans jamais lever d'exception.

    Un corps illisible est lui-même un symptôme : sans le paquet Brotli,
    `requests` renvoie des octets compressés et tout diagnostic devient
    impossible. Le signaler valait bien ce filet.
    """
    try:
        return response.text or ""
    except Exception as exc:  # pragma: no cover - dépend de l'encodage reçu
        raw = getattr(response, "content", b"") or b""
        return (
            f"<corps indécodable ({type(exc).__name__}: {exc}) ; "
            f"{len(raw)} octets bruts, début : {raw[:200]!r}>"
        )


# Trois écritures du code d'erreur cohabitent selon la génération de la page de
# blocage : la classe CSS `cf-error-code` sur les pages actuelles, « error code:
# 1020 » dans le pied de page, et « Error 1020 » sur les pages anciennes.
WAF_CODE_PATTERNS = (
    r"cf-error-code[^>]*>\s*(1\d{3})",
    r"error\s*code[:\s]*(1\d{3})\b",
    r"\berror\s+(1\d{3})\b",
)


#: Une URI de données en base64. La page « Site not reachable » de Vélib' en
#: contient une de 60 ko — l'image de fond — qui à elle seule dépasse la limite
#: de vidage et pousse hors champ le titre, le message et le code d'erreur.
#: Mesuré : corps de 69 341 octets, dont environ 68 000 pour cette seule image.
DATA_URI_PATTERN = re.compile(r"data:[a-z/+.-]+;base64,[A-Za-z0-9+/=\s]{200,}")


def condense_body(body: str) -> tuple[str, int]:
    """Élide de la page les données binaires en base64 et les blancs en excès.

    Le vidage sert à lire un message d'erreur, pas à archiver une image de fond.
    Sans cette condensation, les 4 000 caractères journalisés sont intégralement
    consommés par l'URI de données de la page de blocage Cloudflare, et le code
    d'erreur — la seule information décisive — n'apparaît jamais.

    Args:
        body: Corps de la réponse, en texte.

    Returns:
        Le corps condensé, et le nombre de caractères élidés.
    """
    def _remplacer(correspondance: re.Match[str]) -> str:
        prefixe = correspondance.group(0).split(";", 1)[0]
        return f"{prefixe};base64,<{len(correspondance.group(0))} caractères élidés>"

    condense, remplacements = DATA_URI_PATTERN.subn(_remplacer, body)
    # Les pages du site sont abondamment indentées et truffées de lignes vides ;
    # les réduire dégage de la place sans rien perdre.
    condense = re.sub(r"[ \t]*\n(?:[ \t]*\n)+", "\n", condense)
    condense = re.sub(r"[ \t]{4,}", "  ", condense)
    return condense, len(body) - len(condense)


def find_waf_error_code(body: str) -> str | None:
    """Extrait le code d'erreur WAF Cloudflare du corps d'une page de blocage.

    Args:
        body: Corps de la réponse, en texte.

    Returns:
        Le code à quatre chiffres, ou None s'il est absent.
    """
    for pattern in WAF_CODE_PATTERNS:
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def find_ray_id(response: Any, body: str) -> str | None:
    """Extrait le Ray ID Cloudflare, identifiant unique de la requête refusée.

    C'est la seule référence exploitable pour retrouver la requête dans le
    tableau de bord Cloudflare — utile si le blocage doit être discuté avec
    l'exploitant du site.
    """
    try:
        header = response.headers.get("cf-ray")
    except Exception:  # pragma: no cover
        header = None
    if header:
        return str(header)
    match = re.search(r"Cloudflare Ray ID:\s*</strong>?\s*([0-9a-f]+)", body, re.IGNORECASE)
    if not match:
        match = re.search(r"ray id:?\s*([0-9a-f]{16,})", body, re.IGNORECASE)
    return match.group(1) if match else None


def describe_response(
    response: Any,
    label: str = "",
    session: HttpSession | None = None,
    body_chars: int | None = None,
) -> str:
    """Construit le vidage de diagnostic d'une réponse HTTP.

    Tout ce qui permet de trancher entre « Cloudflare a bloqué » et « le site a
    répondu autre chose que prévu » figure dans ce bloc : le moteur employé,
    l'intégralité des en-têtes de réponse, le code d'erreur WAF, le Ray ID, les
    cookies acquis, et le corps brut. Les corps de *requête* en sont
    volontairement absents : celui du POST `/login` contient le mot de passe.

    Args:
        response: Réponse à décrire.
        label: Étiquette libre rappelant l'étape en cours.
        session: Session émettrice, pour nommer le moteur et lister ses cookies.
        body_chars: Longueur de corps journalisée. Par défaut `DUMP_BODY_CHARS`,
            surchargeable par `VELIB_HTTP_DUMP_BODY_CHARS`.

    Returns:
        Un bloc de texte multiligne, prêt à être journalisé.
    """
    if body_chars is None:
        try:
            body_chars = int(os.environ.get("VELIB_HTTP_DUMP_BODY_CHARS") or DUMP_BODY_CHARS)
        except ValueError:
            body_chars = DUMP_BODY_CHARS

    request = getattr(response, "request", None)
    method = getattr(request, "method", "?") or "?"
    status = getattr(response, "status_code", "?")
    final_url = getattr(response, "url", "?")

    body = _decode_body(response)
    try:
        raw_length = len(getattr(response, "content", b"") or b"")
    except Exception:  # pragma: no cover
        raw_length = len(body.encode("utf-8", "replace"))

    lines: list[str] = [
        "",
        "=" * 78,
        f"DIAGNOSTIC HTTP — {label or 'réponse inattendue'}",
        "=" * 78,
        f"Requête       : {method} {getattr(request, 'url', final_url)}",
        f"Statut        : HTTP {status}",
        f"URL finale    : {final_url}",
    ]

    if session is not None:
        lines.append(f"Moteur        : {describe_session(session)}")

    history = getattr(response, "history", None) or []
    if history:
        chaine = " -> ".join(
            f"{getattr(step, 'status_code', '?')} {getattr(step, 'url', '?')}"
            for step in history
        )
        lines.append(f"Redirections  : {chaine} -> {status} {final_url}")

    lines.append("")
    lines.append("--- En-têtes de réponse (intégralité) ---")
    header_lines = [
        f"  {name}: {_mask_header(name, value)}"
        for name, value in _header_items(response)
    ]
    lines.extend(header_lines or ["  (aucun en-tête lisible)"])

    if session is not None:
        noms = cookie_names(session)
        lines.append("")
        lines.append("--- Cookies détenus par la session après cette requête ---")
        lines.append(f"  {', '.join(noms) if noms else '(aucun)'}")

    # Lecture assistée : on nomme ce que les en-têtes et le corps révèlent, pour
    # ne pas laisser l'interprétation à la charge du lecteur des journaux.
    verdict: list[str] = []
    try:
        serveur = (response.headers.get("server") or "").lower()
        mitigated = (response.headers.get("cf-mitigated") or "").lower()
    except Exception:  # pragma: no cover
        serveur, mitigated = "", ""

    if "cloudflare" in serveur:
        verdict.append("En-tête « Server: cloudflare » : la réponse vient du bord Cloudflare.")
    else:
        verdict.append(
            f"En-tête « Server: {serveur or 'absent'} » : la réponse ne semble PAS "
            "émise par Cloudflare — chercher la cause côté applicatif."
        )
    if mitigated:
        verdict.append(f"En-tête « cf-mitigated: {mitigated} » : action de Bot Management.")

    waf = find_waf_error_code(body)
    if waf:
        verdict.append(
            f"Code d'erreur WAF {waf} — {WAF_ERROR_CODES.get(waf, 'code non répertorié.')}"
        )
    ray = find_ray_id(response, body)
    if ray:
        verdict.append(f"Ray ID : {ray} (référence de la requête chez Cloudflare).")

    lines.append("")
    lines.append("--- Lecture ---")
    lines.extend(f"  {ligne}" for ligne in verdict)

    # Le code WAF et le Ray ID sont cherchés dans le corps INTÉGRAL ci-dessus,
    # avant condensation : rien de ce qui sert au diagnostic ne dépend de la
    # forme condensée, qui n'existe que pour la lisibilité.
    condense, elides = condense_body(body)
    lines.append("")
    lines.append(
        f"--- Corps ({raw_length} octets reçus, "
        f"{min(len(condense), body_chars)} caractères affichés"
        + (f", {elides} élidés (base64, blancs)" if elides else "")
        + ") ---"
    )
    lines.append(condense[:body_chars] if condense else "(corps vide)")
    lines.append("=" * 78)
    lines.append("")
    return "\n".join(lines)


def debug_enabled() -> bool:
    """Indique si `VELIB_HTTP_DEBUG` réclame le vidage de TOUTES les réponses."""
    return (os.environ.get("VELIB_HTTP_DEBUG") or "").strip().lower() in {
        "1", "true", "yes", "oui", "on",
    }


def dump_response(
    response: Any,
    label: str = "",
    session: HttpSession | None = None,
    level: int = logging.ERROR,
) -> None:
    """Journalise le vidage de diagnostic d'une réponse.

    Le niveau est ERROR par défaut : sur un runner, `main.py` journalise en
    INFO, et un diagnostic émis en DEBUG resterait invisible au moment précis
    où on en a besoin.
    """
    logger.log(level, "%s", describe_response(response, label=label, session=session))
