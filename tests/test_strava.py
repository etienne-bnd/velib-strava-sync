# -*- coding: utf-8 -*-
"""Tests de l'authentification OAuth2 et de l'envoi vers Strava."""

from __future__ import annotations

import pytest
import responses

import strava
from strava import (
    StravaDuplicateError,
    StravaError,
    StravaRateLimitError,
    get_access_token,
    upload_to_strava,
)


@pytest.fixture(autouse=True)
def _sans_attente(monkeypatch) -> None:
    """Neutralise les pauses d'interrogation pour garder les tests rapides."""
    monkeypatch.setattr(strava, "UPLOAD_POLL_INTERVAL", 0)


@pytest.fixture
def gpx(tmp_path):
    """Un fichier GPX minimal sur disque."""
    chemin = tmp_path / "temp_trip.gpx"
    chemin.write_text('<?xml version="1.0"?><gpx version="1.1"/>', encoding="utf-8")
    return chemin


# --------------------------------------------------------------------------- #
# Description
# --------------------------------------------------------------------------- #

def test_description_cite_le_depot() -> None:
    """La description doit toujours porter la mention et le lien du dépôt."""
    texte = strava.build_description("https://github.com/u/p")
    assert "synchronisé automatiquement" in texte
    assert "https://github.com/u/p" in texte


def test_description_avec_details() -> None:
    """Le complément s'insère entre la mention et le lien."""
    lignes = strava.build_description("https://github.com/u/p", "2,4 km · 15 min").split("\n")
    assert lignes[1] == "2,4 km · 15 min"
    assert lignes[2].startswith("Code source")


# --------------------------------------------------------------------------- #
# OAuth2
# --------------------------------------------------------------------------- #

@responses.activate
def test_jeton_rafraichi() -> None:
    """Un échange nominal retourne le jeton d'accès."""
    responses.add(
        responses.POST, strava.TOKEN_URL, status=200,
        json={"access_token": "jeton-frais", "expires_at": 1790000000,
              "refresh_token": "rafraichissement"},
    )
    assert get_access_token("123", "secret", "rafraichissement") == "jeton-frais"


@responses.activate
def test_rotation_du_refresh_token_signalee(caplog) -> None:
    """Strava fait tourner le refresh token : il faut en avertir l'utilisateur."""
    responses.add(
        responses.POST, strava.TOKEN_URL, status=200,
        json={"access_token": "a", "refresh_token": "NOUVEAU"},
    )
    with caplog.at_level("WARNING"):
        get_access_token("123", "secret", "ANCIEN")
    assert "NOUVEAU" in caplog.text


@responses.activate
def test_identifiants_refuses() -> None:
    """Un HTTP 400 doit orienter vers les trois secrets à vérifier."""
    responses.add(
        responses.POST, strava.TOKEN_URL, status=400,
        json={"message": "Bad Request", "errors": [{"code": "invalid"}]},
    )
    with pytest.raises(StravaError, match="STRAVA_REFRESH_TOKEN"):
        get_access_token("123", "mauvais", "rafraichissement")


@responses.activate
def test_reponse_sans_access_token() -> None:
    """Une réponse 200 mais incomplète ne doit pas passer inaperçue."""
    responses.add(responses.POST, strava.TOKEN_URL, json={"token_type": "Bearer"}, status=200)
    with pytest.raises(StravaError, match="sans access_token"):
        get_access_token("123", "secret", "rafraichissement")


# --------------------------------------------------------------------------- #
# Envoi
# --------------------------------------------------------------------------- #

@responses.activate
def test_envoi_nominal(gpx) -> None:
    """L'envoi est suivi jusqu'à l'obtention de l'identifiant d'activité."""
    responses.add(responses.POST, strava.UPLOADS_URL, json={"id": 555, "status": "reçu"}, status=201)
    responses.add(
        responses.GET, f"{strava.UPLOADS_URL}/555",
        json={"id": 555, "status": "En cours", "activity_id": None}, status=200,
    )
    responses.add(
        responses.GET, f"{strava.UPLOADS_URL}/555",
        json={"id": 555, "status": "Terminé", "activity_id": 987654}, status=200,
    )

    resultat = upload_to_strava("jeton", gpx, "Vélib' matinal", "description")
    assert resultat["activity_id"] == 987654


@responses.activate
def test_parametres_d_envoi_conformes(gpx) -> None:
    """data_type, activity_type, name et description doivent être transmis."""
    responses.add(responses.POST, strava.UPLOADS_URL, json={"id": 1}, status=201)
    responses.add(
        responses.GET, f"{strava.UPLOADS_URL}/1", json={"activity_id": 2}, status=200
    )

    upload_to_strava("jeton", gpx, "Mon trajet", "Ma description")

    corps = responses.calls[0].request.body.decode("utf-8", errors="replace")
    for attendu in ("gpx", "ride", "Mon trajet", "Ma description"):
        assert attendu in corps
    assert responses.calls[0].request.headers["Authorization"] == "Bearer jeton"


@responses.activate
def test_doublon_detecte_a_l_envoi(gpx) -> None:
    """Un doublon signalé dès le POST lève l'exception dédiée."""
    responses.add(
        responses.POST, strava.UPLOADS_URL, status=400,
        json={"message": "duplicate of activity 123"},
    )
    with pytest.raises(StravaDuplicateError):
        upload_to_strava("jeton", gpx, "Trajet")


@responses.activate
def test_doublon_detecte_a_la_verification(gpx) -> None:
    """Un doublon signalé lors du traitement asynchrone lève la même exception."""
    responses.add(responses.POST, strava.UPLOADS_URL, json={"id": 9}, status=201)
    responses.add(
        responses.GET, f"{strava.UPLOADS_URL}/9",
        json={"id": 9, "error": "duplicate of activity 123"}, status=200,
    )
    with pytest.raises(StravaDuplicateError):
        upload_to_strava("jeton", gpx, "Trajet")


@responses.activate
def test_quota_epuise(gpx) -> None:
    """Un HTTP 429 lève l'exception qui interrompt proprement l'exécution."""
    responses.add(
        responses.POST, strava.UPLOADS_URL, status=429,
        headers={"X-RateLimit-Usage": "200,2000", "X-RateLimit-Limit": "200,2000"},
    )
    with pytest.raises(StravaRateLimitError, match="Quota Strava épuisé"):
        upload_to_strava("jeton", gpx, "Trajet")


@responses.activate
def test_jeton_sans_le_bon_scope(gpx) -> None:
    """Un HTTP 401 doit pointer vers le scope activity:write."""
    responses.add(responses.POST, strava.UPLOADS_URL, status=401, json={})
    with pytest.raises(StravaError, match="activity:write"):
        upload_to_strava("jeton", gpx, "Trajet")


@responses.activate
def test_gpx_rejete_par_strava(gpx) -> None:
    """Un GPX invalide est signalé lors de la vérification asynchrone."""
    responses.add(responses.POST, strava.UPLOADS_URL, json={"id": 3}, status=201)
    responses.add(
        responses.GET, f"{strava.UPLOADS_URL}/3",
        json={"id": 3, "error": "Le fichier est vide ou corrompu"}, status=200,
    )
    with pytest.raises(StravaError, match="rejeté"):
        upload_to_strava("jeton", gpx, "Trajet")


@responses.activate
def test_delai_de_traitement_depasse(gpx, monkeypatch) -> None:
    """Un envoi qui n'aboutit pas dans le délai imparti est signalé."""
    monkeypatch.setattr(strava, "UPLOAD_POLL_MAX_ATTEMPTS", 2)
    responses.add(responses.POST, strava.UPLOADS_URL, json={"id": 4}, status=201)
    responses.add(
        responses.GET, f"{strava.UPLOADS_URL}/4",
        json={"id": 4, "status": "En cours"}, status=200,
    )
    with pytest.raises(StravaError, match="délai imparti"):
        upload_to_strava("jeton", gpx, "Trajet")


def test_fichier_gpx_absent(tmp_path) -> None:
    """Un chemin de GPX inexistant est détecté avant tout appel réseau."""
    with pytest.raises(StravaError, match="introuvable"):
        upload_to_strava("jeton", tmp_path / "absent.gpx", "Trajet")


@responses.activate
def test_envoi_sans_attente(gpx) -> None:
    """`wait_for_completion=False` retourne immédiatement la charge utile."""
    responses.add(responses.POST, strava.UPLOADS_URL, json={"id": 77}, status=201)
    resultat = upload_to_strava("jeton", gpx, "Trajet", wait_for_completion=False)
    assert resultat["id"] == 77
    assert len(responses.calls) == 1


# --------------------------------------------------------------------------- #
# Vérification du scope
# --------------------------------------------------------------------------- #

@responses.activate
def test_scope_insuffisant_detecte_au_rafraichissement() -> None:
    """Un jeton sans `activity:write` doit échouer tout de suite, pas à l'envoi.

    Sans cette vérification, l'absence du scope ne se manifeste qu'au HTTP 401
    de `POST /uploads` — après avoir consommé une requête OpenRouteService et
    écrit un GPX pour rien, et sous un message d'erreur peu parlant.
    """
    responses.add(
        responses.POST, strava.TOKEN_URL, status=200,
        json={"access_token": "jeton", "scope": "read", "expires_at": 1790000000},
    )
    with pytest.raises(strava.StravaScopeError) as info:
        get_access_token("277302", "secret", "rafraichissement")

    message = str(info.value)
    assert "activity:write" in message
    assert "277302" in message           # l'URL de réémission est prête à l'emploi
    assert "oauth/authorize" in message


@responses.activate
def test_scope_suffisant_accepte() -> None:
    """Un jeton portant `activity:write` passe sans encombre."""
    responses.add(
        responses.POST, strava.TOKEN_URL, status=200,
        json={"access_token": "jeton", "scope": "read,activity:write,activity:read_all"},
    )
    assert get_access_token("123", "secret", "rafraichissement") == "jeton"


@responses.activate
def test_scope_absent_de_la_reponse_ne_bloque_pas() -> None:
    """Strava n'a pas toujours renvoyé ce champ : son absence ne doit pas bloquer."""
    responses.add(
        responses.POST, strava.TOKEN_URL, status=200, json={"access_token": "jeton"}
    )
    assert get_access_token("123", "secret", "rafraichissement") == "jeton"


# --------------------------------------------------------------------------- #
# Relecture d'une activité créée
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("scope", "attendu"),
    [
        ("read,activity:write", set()),
        ("read,activity:write,activity:read_all", {"read", "activity:write", "activity:read_all"}),
        ("activity:write activity:read", {"activity:write", "activity:read"}),
        ("", set()),
        (None, set()),
    ],
)
def test_decomposition_des_scopes(scope, attendu) -> None:
    """Strava sépare les scopes tantôt par virgules, tantôt par espaces."""
    resultat = strava.parse_scopes(scope)
    if attendu:
        assert resultat == attendu
    else:
        assert resultat in (set(), {"read", "activity:write"})


@responses.activate
def test_relecture_d_activite() -> None:
    """Une activité relisible retourne sa description complète."""
    responses.add(
        responses.GET, f"{strava.API_BASE}/activities/20059454116", status=200,
        json={"id": 20059454116, "name": "Vélib' nocturne", "distance": 2640.0,
              "elapsed_time": 855, "sport_type": "Ride"},
    )
    activite = strava.fetch_activity("jeton", 20059454116)
    assert activite["name"] == "Vélib' nocturne"


@pytest.mark.parametrize("statut", [401, 404])
@responses.activate
def test_relecture_sans_scope_retourne_none(statut) -> None:
    """Sans scope de lecture, Strava répond 404 : ce n'est pas une erreur fatale.

    L'envoi a pu parfaitement réussir ; seule la vérification est indisponible.
    Le code doit donc retourner None plutôt que lever.
    """
    responses.add(
        responses.GET, f"{strava.API_BASE}/activities/1", status=statut, json={}
    )
    assert strava.fetch_activity("jeton", 1) is None


@responses.activate
def test_absence_de_scope_de_lecture_signalee(caplog) -> None:
    """Un jeton sans scope de lecture est signalé, sans bloquer l'envoi."""
    responses.add(
        responses.POST, strava.TOKEN_URL, status=200,
        json={"access_token": "jeton", "scope": "read,activity:write"},
    )
    with caplog.at_level("INFO"):
        assert get_access_token("277302", "secret", "rafraichissement") == "jeton"
    assert "aucun scope de lecture" in caplog.text
    assert "activity:read_all" in caplog.text
