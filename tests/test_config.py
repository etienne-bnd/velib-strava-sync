# -*- coding: utf-8 -*-
"""Tests du chargement de la configuration."""

from __future__ import annotations

import pytest

import config
from config import ConfigError, load_config

VARIABLES = [
    "VELIB_USERNAME", "VELIB_EMAIL", "VELIB_PASSWORD", "STRAVA_CLIENT_ID",
    "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN", "ORS_API_KEY",
    "PROJECT_REPO_URL", "MAX_TRIPS_PER_RUN", "MAX_TRIP_AGE_DAYS", "DRY_RUN",
    "STATE_FILE", "GPX_FILE",
]

COMPLET = {
    "VELIB_USERNAME": "a@b.fr", "VELIB_PASSWORD": "motdepasse",
    "STRAVA_CLIENT_ID": "123", "STRAVA_CLIENT_SECRET": "secret",
    "STRAVA_REFRESH_TOKEN": "rafraichissement", "ORS_API_KEY": "cle-ors",
}


@pytest.fixture(autouse=True)
def _environnement_propre(monkeypatch):
    """Isole chaque test de l'environnement réel et du fichier .env du projet."""
    for nom in VARIABLES:
        monkeypatch.delenv(nom, raising=False)
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: False)


def test_configuration_complete(monkeypatch) -> None:
    """Toutes les variables présentes : la configuration est construite."""
    for cle, valeur in COMPLET.items():
        monkeypatch.setenv(cle, valeur)
    reglages = load_config()
    assert reglages.velib_username == "a@b.fr"
    assert reglages.max_trips_per_run == 25
    assert reglages.dry_run is False


@pytest.mark.parametrize("manquante", sorted(COMPLET))
def test_variable_manquante_signalee(monkeypatch, manquante) -> None:
    """Chaque variable obligatoire absente doit être nommée dans l'erreur."""
    for cle, valeur in COMPLET.items():
        if cle != manquante:
            monkeypatch.setenv(cle, valeur)
    with pytest.raises(ConfigError, match=manquante):
        load_config()


def test_velib_email_accepte_en_repli(monkeypatch) -> None:
    """VELIB_EMAIL du .env initial reste accepté à la place de VELIB_USERNAME."""
    for cle, valeur in COMPLET.items():
        if cle != "VELIB_USERNAME":
            monkeypatch.setenv(cle, valeur)
    monkeypatch.setenv("VELIB_EMAIL", "repli@b.fr")
    assert load_config().velib_username == "repli@b.fr"


def test_velib_username_prime_sur_velib_email(monkeypatch) -> None:
    """Si les deux sont définies, VELIB_USERNAME l'emporte."""
    for cle, valeur in COMPLET.items():
        monkeypatch.setenv(cle, valeur)
    monkeypatch.setenv("VELIB_EMAIL", "repli@b.fr")
    assert load_config().velib_username == "a@b.fr"


def test_variable_vide_traitee_comme_absente(monkeypatch) -> None:
    """Un secret GitHub non configuré arrive comme chaîne vide, pas absent."""
    for cle, valeur in COMPLET.items():
        monkeypatch.setenv(cle, valeur)
    monkeypatch.setenv("ORS_API_KEY", "   ")
    with pytest.raises(ConfigError, match="ORS_API_KEY"):
        load_config()


@pytest.mark.parametrize(
    ("valeur", "attendu"),
    [("1", True), ("true", True), ("oui", True), ("0", False), ("non", False), ("", False)],
)
def test_lecture_des_booleens(monkeypatch, valeur, attendu) -> None:
    """DRY_RUN accepte les formes courantes, en français comme en anglais."""
    for cle, v in COMPLET.items():
        monkeypatch.setenv(cle, v)
    monkeypatch.setenv("DRY_RUN", valeur)
    assert load_config().dry_run is attendu


def test_entier_invalide_signale(monkeypatch) -> None:
    """Une valeur numérique illisible doit être signalée, pas silencieusement ignorée."""
    for cle, valeur in COMPLET.items():
        monkeypatch.setenv(cle, valeur)
    monkeypatch.setenv("MAX_TRIPS_PER_RUN", "beaucoup")
    with pytest.raises(ConfigError, match="doit être un entier"):
        load_config()


def test_url_du_depot_par_defaut(monkeypatch) -> None:
    """Sans PROJECT_REPO_URL, une valeur repère est utilisée."""
    for cle, valeur in COMPLET.items():
        monkeypatch.setenv(cle, valeur)
    assert load_config().repo_url == config.DEFAULT_REPO_URL
