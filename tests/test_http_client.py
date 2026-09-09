# -*- coding: utf-8 -*-
"""Tests du transport HTTP : choix du moteur et vidage de diagnostic."""

from __future__ import annotations

import logging

import pytest
import requests
import responses

import http_client


# --------------------------------------------------------------------------- #
# Choix du moteur
# --------------------------------------------------------------------------- #

def _sans_curl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simule une installation dépourvue de curl_cffi."""
    monkeypatch.setattr(http_client, "_curl_module", lambda: None)


def test_moteur_explicite_requests(monkeypatch) -> None:
    """`requests` demandé explicitement est retenu, même si curl est disponible."""
    monkeypatch.delenv("VELIB_HTTP_BACKEND", raising=False)
    assert http_client.resolve_backend("requests") == http_client.BACKEND_REQUESTS


def test_auto_retient_curl_quand_il_est_disponible(monkeypatch) -> None:
    """En mode auto, curl_cffi l'emporte : c'est lui qui passe le pare-feu."""
    monkeypatch.setattr(http_client, "_curl_module", lambda: object())
    assert http_client.resolve_backend("auto") == http_client.BACKEND_CURL


def test_auto_retombe_sur_requests_sans_curl(monkeypatch) -> None:
    """En mode auto, l'absence de curl_cffi n'empêche pas de fonctionner."""
    _sans_curl(monkeypatch)
    assert http_client.resolve_backend("auto") == http_client.BACKEND_REQUESTS


def test_curl_exige_mais_absent_echoue_bruyamment(monkeypatch) -> None:
    """Un repli SILENCIEUX sur `requests` redonnerait le HTTP 403 sans l'expliquer.

    D'où l'échec immédiat : en CI, `VELIB_HTTP_BACKEND=curl` est une exigence,
    pas une préférence.
    """
    _sans_curl(monkeypatch)
    with pytest.raises(http_client.BackendUnavailable, match="curl_cffi"):
        http_client.resolve_backend("curl")


def test_moteur_inconnu_retombe_sur_auto(monkeypatch, caplog) -> None:
    """Une valeur erronée est signalée puis ignorée, sans faire échouer le script."""
    _sans_curl(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="http_client"):
        assert http_client.resolve_backend("chromium") == http_client.BACKEND_REQUESTS
    assert "inconnu" in caplog.text


def test_variable_d_environnement_prise_en_compte(monkeypatch) -> None:
    """`VELIB_HTTP_BACKEND` pilote le choix en l'absence d'argument explicite."""
    monkeypatch.setenv("VELIB_HTTP_BACKEND", "requests")
    assert http_client.resolve_backend() == http_client.BACKEND_REQUESTS


def test_cible_d_imitation_surchargeable(monkeypatch) -> None:
    """`VELIB_IMPERSONATE` permet de changer de navigateur imité sans toucher au code."""
    monkeypatch.setenv("VELIB_IMPERSONATE", "firefox")
    assert http_client.impersonate_target() == "firefox"
    monkeypatch.delenv("VELIB_IMPERSONATE")
    assert http_client.impersonate_target() == http_client.DEFAULT_IMPERSONATE


# --------------------------------------------------------------------------- #
# Construction des sessions
# --------------------------------------------------------------------------- #

def test_session_requests_porte_les_en_tetes_navigateur() -> None:
    """Le moteur `requests` n'imite rien : les en-têtes doivent être écrits à la main."""
    session = http_client.build_session("requests")
    assert isinstance(session, requests.Session)
    assert session.velib_backend == http_client.BACKEND_REQUESTS
    assert session.velib_impersonate is None
    assert "Chrome" in session.headers["User-Agent"]


def test_en_tetes_du_moteur_requests_coherents() -> None:
    """User-Agent, Sec-Ch-Ua et Sec-Ch-Ua-Platform doivent concorder.

    Une incohérence entre ces trois en-têtes est un signal fort pour Cloudflare :
    aucun navigateur réel n'annonce Chrome 152 sur Windows dans l'un et une
    autre version ou un autre système dans les suivants.
    """
    entetes = http_client.BROWSER_HEADERS
    assert "Windows" in entetes["User-Agent"]
    assert "Windows" in entetes["Sec-Ch-Ua-Platform"]
    version = "152"
    assert version in entetes["User-Agent"]
    assert version in entetes["Sec-Ch-Ua"]


def test_le_moteur_curl_ne_reinjecte_pas_les_en_tetes_a_la_main() -> None:
    """`IMPERSONATED_HEADERS` ne doit JAMAIS porter User-Agent ni Sec-Ch-Ua.

    curl_cffi pose les en-têtes du Chrome imité, mais ses valeurs par défaut ne
    remplacent pas celles fournies par l'appelant. Y ajouter notre User-Agent
    « Windows » produirait donc un User-Agent Windows sur une empreinte macOS,
    avec un `Sec-Ch-Ua-Platform: "macOS"` en contradiction — soit précisément
    l'incohérence que l'imitation cherche à éviter. Ce test verrouille ce piège.
    """
    interdits = {"user-agent", "sec-ch-ua", "sec-ch-ua-platform", "accept-encoding"}
    presents = {nom.lower() for nom in http_client.IMPERSONATED_HEADERS}
    assert not (presents & interdits)


def test_session_curl_declare_sa_cible() -> None:
    """Le moteur `curl` mémorise la cible imitée, que le diagnostic cite ensuite."""
    pytest.importorskip("curl_cffi")
    session = http_client.build_session("curl")
    try:
        assert session.velib_backend == http_client.BACKEND_CURL
        assert session.velib_impersonate == http_client.impersonate_target()
        assert "chrome" in http_client.describe_session(session)
    finally:
        session.close()


def test_erreurs_reseau_couvrent_les_deux_moteurs() -> None:
    """Une coupure réseau doit être rattrapée quel que soit le moteur employé."""
    erreurs = http_client.network_errors()
    assert requests.RequestException in erreurs
    if http_client.curl_available():
        from curl_cffi.requests import exceptions as curl_exceptions

        assert any(issubclass(curl_exceptions.ConnectionError, e) for e in erreurs)


@pytest.mark.parametrize(
    ("valeur", "attendu"),
    [("1", True), ("true", True), ("oui", True), ("0", False), ("", False), ("non", False)],
)
def test_lecture_du_drapeau_de_debogage(monkeypatch, valeur, attendu) -> None:
    """`VELIB_HTTP_DEBUG` accepte les écritures usuelles d'un booléen."""
    monkeypatch.setenv("VELIB_HTTP_DEBUG", valeur)
    assert http_client.debug_enabled() is attendu


# --------------------------------------------------------------------------- #
# Codes d'erreur WAF
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("corps", "attendu"),
    [
        ('<span class="cf-error-code">1020</span>', "1020"),
        ("Error code: 1015", "1015"),
        ("Error 1010 Ray ID: abc", "1010"),
        ("<html>page normale</html>", None),
    ],
)
def test_extraction_du_code_waf(corps, attendu) -> None:
    """Les trois écritures du code d'erreur Cloudflare sont reconnues."""
    assert http_client.find_waf_error_code(corps) == attendu


def test_les_codes_waf_utiles_sont_documentes() -> None:
    """1020 et 1010 sont les deux codes que ce projet doit savoir expliquer."""
    assert "pare-feu" in http_client.WAF_ERROR_CODES["1020"]
    assert "curl-impersonate" in http_client.WAF_ERROR_CODES["1010"]


# --------------------------------------------------------------------------- #
# Vidage de diagnostic
# --------------------------------------------------------------------------- #

PAGE_BLOQUEE = (
    "<!DOCTYPE html><html><head><title>Access denied</title></head><body>"
    '<h1>Sorry, you have been blocked</h1><span class="cf-error-code">1020</span>'
    "<div>Cloudflare Ray ID: <strong>9a1b2c3d4e5f6789</strong></div></body></html>"
)


@responses.activate
def _reponse_bloquee() -> tuple[object, object]:
    """Fabrique une réponse 403 façon page de blocage Cloudflare."""
    responses.add(
        responses.GET, "https://www.velib-metropole.fr/login",
        body=PAGE_BLOQUEE, status=403,
        headers={
            "Server": "cloudflare",
            "Cf-Ray": "9a1b2c3d4e5f6789-CDG",
            "Cf-Mitigated": "challenge",
            "Set-Cookie": "__cf_bm=valeur-tres-secrete; path=/; HttpOnly; Secure",
        },
    )
    session = http_client.build_session("requests")
    return session, session.request("GET", "https://www.velib-metropole.fr/login")


def test_le_vidage_montre_tout_ce_qui_permet_de_conclure() -> None:
    """Le diagnostic doit se lire seul dans les journaux d'un runner.

    Sur un runner, il n'y a ni débogueur ni session interactive : le bloc
    journalisé est la seule source d'information. Il doit donc porter le statut,
    l'intégralité des en-têtes, le code WAF, le Ray ID et le corps brut.
    """
    session, reponse = _reponse_bloquee()
    vidage = http_client.describe_response(reponse, label="GET /login", session=session)

    assert "HTTP 403" in vidage
    assert "server: cloudflare" in vidage.lower()
    assert "cf-mitigated: challenge" in vidage.lower()
    assert "1020" in vidage
    assert "pare-feu" in vidage
    assert "9a1b2c3d4e5f6789" in vidage
    assert "Sorry, you have been blocked" in vidage
    assert "requests" in vidage


def test_le_vidage_masque_la_valeur_des_cookies() -> None:
    """Les journaux d'un dépôt public sont publics : aucune valeur de cookie.

    Le nom du cookie est conservé — il dit s'il s'agit de `__cf_bm`, de
    `cf_clearance` ou de la session applicative — ainsi que ses attributs.
    """
    session, reponse = _reponse_bloquee()
    vidage = http_client.describe_response(reponse, session=session)

    assert "valeur-tres-secrete" not in vidage
    assert "__cf_bm=<valeur masquée>" in vidage
    assert "HttpOnly" in vidage


def test_le_vidage_liste_les_cookies_de_session_sans_leurs_valeurs() -> None:
    """Savoir si `__cf_bm` a bien été posé est le premier réflexe de diagnostic."""
    session, reponse = _reponse_bloquee()
    vidage = http_client.describe_response(reponse, session=session)
    assert "__cf_bm" in vidage
    assert "valeur-tres-secrete" not in vidage


@responses.activate
def test_une_reponse_non_cloudflare_est_annoncee_comme_telle() -> None:
    """Distinguer « Cloudflare a bloqué » de « le site a répondu autre chose »."""
    responses.add(
        responses.GET, "https://www.velib-metropole.fr/login",
        body="<html>maintenance</html>", status=503, headers={"Server": "nginx"},
    )
    session = http_client.build_session("requests")
    reponse = session.request("GET", "https://www.velib-metropole.fr/login")
    vidage = http_client.describe_response(reponse, session=session)
    assert "ne semble PAS" in vidage


@responses.activate
def test_un_corps_indecodable_ne_fait_pas_echouer_le_vidage() -> None:
    """Un corps illisible est un symptôme, pas une raison de perdre le diagnostic.

    Sans le paquet Brotli, `requests` renvoie des octets compressés : le vidage
    doit malgré tout produire les en-têtes, qui suffisent souvent à conclure.
    """
    responses.add(
        responses.GET, "https://www.velib-metropole.fr/login",
        body=b"\x1f\x8b\x08 octets bruts", status=403,
        headers={"Server": "cloudflare", "Content-Encoding": "br"},
    )
    session = http_client.build_session("requests")
    reponse = session.request("GET", "https://www.velib-metropole.fr/login")
    vidage = http_client.describe_response(reponse, session=session)
    assert "server: cloudflare" in vidage.lower()
    assert "content-encoding: br" in vidage.lower()


def test_les_donnees_base64_sont_elidees() -> None:
    """L'image de fond de la page de blocage ne doit pas noyer le diagnostic.

    Mesuré sur la vraie page « Site not reachable » de Vélib' : 69 341 octets,
    dont environ 68 000 pour une seule URI de données en base64. Sans
    condensation, les 4 000 caractères journalisés y passent intégralement et le
    code d'erreur WAF — la seule information décisive — reste invisible.
    """
    corps = (
        "<html><title>Site not reachable</title>"
        "<style>body{background-image:url('data:image/png;base64,"
        + "iVBORw0KGgo" * 400
        + "')}</style>"
        '<span class="cf-error-code">1020</span></html>'
    )
    condense, elides = http_client.condense_body(corps)

    assert elides > 4000
    assert len(condense) < 400
    assert "Site not reachable" in condense
    assert "1020" in condense
    assert "caractères élidés" in condense
    # La détection travaille sur le corps intégral : la condensation ne sert
    # qu'à la lisibilité et ne doit rien lui retirer.
    assert http_client.find_waf_error_code(corps) == "1020"


def test_un_corps_sans_base64_traverse_sans_perte() -> None:
    """Une page ordinaire ne doit pas être mutilée par la condensation."""
    corps = '<html><input name="_csrf_token" value="jeton"/></html>'
    condense, elides = http_client.condense_body(corps)
    assert condense == corps
    assert elides == 0


@responses.activate
def test_le_corps_journalise_est_plafonne(monkeypatch) -> None:
    """Un corps de 300 ko noierait les journaux : la troncature est réglable."""
    monkeypatch.setenv("VELIB_HTTP_DUMP_BODY_CHARS", "50")
    responses.add(
        responses.GET, "https://www.velib-metropole.fr/", body="x" * 5000, status=403,
    )
    session = http_client.build_session("requests")
    reponse = session.request("GET", "https://www.velib-metropole.fr/")
    vidage = http_client.describe_response(reponse, session=session)
    assert "5000 octets reçus" in vidage
    assert "x" * 51 not in vidage


def test_le_vidage_est_journalise_en_erreur(caplog) -> None:
    """Le niveau ERROR est indispensable : `main.py` journalise en INFO.

    Un diagnostic émis en DEBUG serait invisible au moment précis où on en a
    besoin, c'est-à-dire dans les journaux d'une exécution CI en échec.
    """
    session, reponse = _reponse_bloquee()
    with caplog.at_level(logging.DEBUG, logger="http_client"):
        http_client.dump_response(reponse, label="test", session=session)

    enregistrements = [r for r in caplog.records if r.name == "http_client"]
    assert len(enregistrements) == 1
    assert enregistrements[0].levelno == logging.ERROR
    assert "DIAGNOSTIC HTTP" in enregistrements[0].getMessage()
