# -*- coding: utf-8 -*-
"""Tests de la normalisation et du parcours de l'API Vélib'."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import responses

import http_client
import velib


# --------------------------------------------------------------------------- #
# Analyse des dates
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("valeur", "iso_attendu"),
    [
        ("2026-09-05T09:15:00+02:00", "2026-09-05T07:15:00+00:00"),
        ("2026-09-05T09:15:00+0200", "2026-09-05T07:15:00+00:00"),  # sans deux-points
        ("2026-09-05T07:15:00Z", "2026-09-05T07:15:00+00:00"),
        (1757056500, "2025-09-05T07:15:00+00:00"),                  # epoch secondes
        (1757056500000, "2025-09-05T07:15:00+00:00"),               # epoch millisecondes
        ("05/09/2026 09:15", "2026-09-05T07:15:00+00:00"),          # format français
    ],
)
def test_analyse_des_formats_de_date(valeur, iso_attendu) -> None:
    """Les six formes de date rencontrées sont toutes ramenées à de l'UTC."""
    assert velib._parse_datetime(valeur).isoformat() == iso_attendu


@pytest.mark.parametrize("valeur", [None, "", "pas une date", [], {}])
def test_dates_inexploitables(valeur) -> None:
    """Une valeur non interprétable donne None, jamais une exception."""
    assert velib._parse_datetime(valeur) is None


# --------------------------------------------------------------------------- #
# Analyse des durées
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("valeur", "attendu"), [(14, 14), ("14", 14), ("12:30", 750), ("1:02:03", 3723), (None, None)]
)
def test_analyse_des_durees(valeur, attendu) -> None:
    """Les durées numériques et au format « MM:SS » sont converties en secondes."""
    assert velib._parse_duration_seconds(valeur) == attendu


def test_quantity_est_interprete_en_secondes() -> None:
    """`quantity` est exprimé en SECONDES, pas en minutes.

    L'enregistrement le déclare lui-même via `ratingUnitDescription: "second"`,
    et sur la capture HAR de référence `quantity=1593` correspond à un trajet
    de 27 min. L'interpréter en minutes produirait des activités 60× trop
    longues, que Strava rejetterait ou afficherait comme aberrantes.
    """
    trip = velib.normalise_trip(
        {"id": 1, "startDate": "2026-09-05T07:00:00Z", "quantity": 1593}
    )
    assert trip.duration_seconds == 1593


def test_duree_calculee_depuis_end_date() -> None:
    """Quand `endDate` est présent, la durée réelle prime sur `quantity`.

    `quantity` porte un arrondi de facturation de 1 à 4 secondes ; l'écart
    entre les deux horodatages est la valeur exacte.
    """
    trip = velib.normalise_trip({
        "id": 1,
        "startDate": "2026-09-04T03:34:17Z",
        "endDate": "2026-09-04T04:00:53Z",
        "quantity": 1593,
    })
    assert trip.duration_seconds == 1596  # 26 min 36 s


def test_end_date_incoherente_ignoree() -> None:
    """Une `endDate` antérieure au départ ne doit pas donner une durée négative."""
    trip = velib.normalise_trip({
        "id": 1, "startDate": "2026-09-04T04:00:00Z",
        "endDate": "2026-09-04T03:00:00Z", "quantity": 900,
    })
    assert trip.duration_seconds == 900  # repli sur quantity


# --------------------------------------------------------------------------- #
# Filtrage
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("quantity", [0, -1, None])
def test_les_trajets_sans_duree_sont_ecartes(quantity) -> None:
    """Un trajet de durée nulle ou négative n'est pas retourné."""
    record = {"courseId": 1, "startDate": "2026-09-05T07:00:00Z", "quantity": quantity}
    assert velib.normalise_trip(record) is None


@pytest.mark.parametrize("statut", ["CANCELLED", "cancelled", "REFUSED", "error"])
def test_les_statuts_invalides_sont_ecartes(statut) -> None:
    """Un trajet au statut d'annulation n'est pas retourné."""
    record = {
        "courseId": 1, "startDate": "2026-09-05T07:00:00Z",
        "quantity": 10, "status": statut,
    }
    assert velib.normalise_trip(record) is None


def test_statut_absent_traite_comme_valide() -> None:
    """L'API ne renseigne pas toujours le statut : son absence ne rejette pas."""
    record = {"courseId": 1, "startDate": "2026-09-05T07:00:00Z", "quantity": 10}
    assert velib.normalise_trip(record) is not None


def test_un_enregistrement_non_dictionnaire_est_ignore() -> None:
    """Un élément inattendu dans la liste ne fait pas planter la normalisation."""
    assert velib.normalise_trip("ceci n'est pas un trajet") is None


# --------------------------------------------------------------------------- #
# Identifiants et champs alternatifs
# --------------------------------------------------------------------------- #

def test_identifiant_de_repli_derive_de_la_date() -> None:
    """Sans identifiant natif, la clé combine date de départ et stations."""
    record = {
        "startDate": "2026-09-05T07:00:00Z", "quantity": 10,
        "departureStationId": "16107", "arrivalStationId": "07001",
    }
    assert velib.normalise_trip(record).trip_id == "20260905T070000Z-16107-07001"


def test_noms_de_champs_alternatifs() -> None:
    """Le schéma de l'API n'étant pas stable, plusieurs noms sont acceptés."""
    record = {
        "id": 7, "operationDate": "2026-09-05T07:00:00Z", "duration": 900,
        "startStationId": "A", "endStationId": "B",
    }
    trip = velib.normalise_trip(record)
    assert (trip.trip_id, trip.duration_seconds) == ("7", 900)
    assert (trip.departure_station_id, trip.arrival_station_id) == ("A", "B")


@pytest.mark.parametrize(
    ("valeur", "attendu"),
    [("ELECTRICAL", "electrical"), ("mechanical", "mechanical"),
     (True, "electrical"), (False, "mechanical"), ("inconnu", None)],
)
def test_normalisation_du_type_de_velo(valeur, attendu) -> None:
    """Les libellés hétérogènes du type de vélo sont ramenés à deux valeurs."""
    assert velib._normalise_bike_type(valeur) == attendu


# --------------------------------------------------------------------------- #
# Détection Cloudflare et jeton CSRF
# --------------------------------------------------------------------------- #

def test_extraction_du_jeton_csrf() -> None:
    """Le jeton CSRF est extrait du champ caché du formulaire."""
    html = '<form><input type="hidden" name="_csrf_token" value="abc123"/></form>'
    assert velib._extract_csrf_token(html) == "abc123"


def test_jeton_csrf_absent_leve_une_erreur() -> None:
    """Un formulaire modifié doit produire un message explicite, pas un None."""
    with pytest.raises(velib.VelibError, match="_csrf_token introuvable"):
        velib._extract_csrf_token("<form></form>")


def test_captcha_detecte() -> None:
    """La présence d'un reCAPTCHA est signalée avant toute tentative."""
    with pytest.raises(velib.VelibError, match="reCAPTCHA"):
        velib._warn_if_captcha('<div class="g-recaptcha"></div>')


@responses.activate
def test_challenge_cloudflare_detecte() -> None:
    """Un challenge anti-bot lève l'exception dédiée, pas une erreur générique."""
    responses.add(
        responses.GET, velib.LOGIN_URL,
        body="<html><title>Just a moment...</title></html>",
        status=403, headers={"cf-mitigated": "challenge"},
    )
    session = velib.build_session()
    with pytest.raises(velib.CloudflareChallenge):
        velib._request(session, "GET", velib.LOGIN_URL)


# --------------------------------------------------------------------------- #
# Parcours complet, avec HTTP simulé
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _sans_amorcage_lent(monkeypatch) -> None:
    """Supprime la pause d'amorçage pour garder les tests rapides."""
    monkeypatch.setattr(velib, "WARMUP_DELAY", 0)


def _enregistrer_amorcage() -> None:
    """Simule la page d'accueil, que `login` visite avant `/login`."""
    responses.add(responses.GET, f"{velib.BASE_URL}/", body="<html>accueil</html>", status=200)


@responses.activate
def test_amorcage_visite_l_accueil_avant_login() -> None:
    """`login` DOIT visiter l'accueil avant `/login`.

    Cloudflare Bot Management refuse une requête vers /login dépourvue du
    cookie `__cf_bm` : mesuré, /login à froid renvoie un HTTP 403 systématique,
    et 200 après une visite de l'accueil sur la même session. Supprimer cette
    visite casserait la connexion en production sans qu'aucun autre test ne
    le détecte.
    """
    _enregistrer_amorcage()
    responses.add(responses.GET, velib.LOGIN_URL,
                  body='<input name="_csrf_token" value="jeton"/>', status=200)
    responses.add(responses.POST, velib.LOGIN_URL, status=302,
                  headers={"Location": velib.ACCOUNT_URL})
    responses.add(responses.GET, velib.ACCOUNT_URL, body="ok", status=200)

    velib.login(velib.build_session(), "a@b.fr", "motdepasse")

    assert responses.calls[0].request.url.rstrip("/") == velib.BASE_URL
    assert responses.calls[1].request.url == velib.LOGIN_URL


@responses.activate
def test_amorcage_en_echec_ne_bloque_pas(caplog) -> None:
    """Un accueil indisponible est signalé mais n'interrompt pas la connexion."""
    responses.add(responses.GET, f"{velib.BASE_URL}/", body="indisponible", status=404)
    responses.add(responses.GET, velib.LOGIN_URL,
                  body='<input name="_csrf_token" value="jeton"/>', status=200)
    responses.add(responses.POST, velib.LOGIN_URL, status=302,
                  headers={"Location": velib.ACCOUNT_URL})
    responses.add(responses.GET, velib.ACCOUNT_URL, body="ok", status=200)

    with caplog.at_level("WARNING"):
        velib.login(velib.build_session(), "a@b.fr", "motdepasse")
    assert "amorçage" in caplog.text


@responses.activate
def test_connexion_reussie() -> None:
    """Une connexion valide suit la redirection vers /private/account."""
    _enregistrer_amorcage()
    responses.add(
        responses.GET, velib.LOGIN_URL,
        body='<input name="_csrf_token" value="jeton"/>', status=200,
    )
    responses.add(
        responses.POST, velib.LOGIN_URL, status=302,
        headers={"Location": velib.ACCOUNT_URL},
    )
    responses.add(responses.GET, velib.ACCOUNT_URL, body="<html>compte</html>", status=200)

    session = velib.build_session()
    velib.login(session, "a@b.fr", "motdepasse")  # ne doit pas lever


@responses.activate
def test_connexion_refusee(monkeypatch) -> None:
    """Des identifiants refusés renvoient sur /login : l'erreur est explicite."""
    monkeypatch.setattr(velib, "RETRY_BACKOFF", 0)
    _enregistrer_amorcage()
    responses.add(
        responses.GET, velib.LOGIN_URL,
        body='<input name="_csrf_token" value="jeton"/>', status=200,
    )
    responses.add(responses.POST, velib.LOGIN_URL, body="Identifiants invalides", status=200)

    session = velib.build_session()
    with pytest.raises(velib.VelibError, match="Authentification refusée"):
        velib.login(session, "a@b.fr", "mauvais")


@responses.activate
def test_pagination_de_l_historique(monkeypatch) -> None:
    """Les pages successives sont concaténées jusqu'au total annoncé."""
    monkeypatch.setattr(velib, "PAGE_DELAY", 0)
    page_1 = {
        "actionStatus": {"status": "SUCCESS"},
        "paging": {"totalNumberOfRecords": 3},
        "walletOperations": [{"courseId": i} for i in range(2)],
    }
    page_2 = {
        "actionStatus": {"status": "SUCCESS"},
        "paging": {"totalNumberOfRecords": 3},
        "walletOperations": [{"courseId": 2}],
    }
    responses.add(responses.GET, velib.COURSE_LIST_URL, json=page_1, status=200)
    responses.add(responses.GET, velib.COURSE_LIST_URL, json=page_2, status=200)

    records = velib.fetch_courses(velib.build_session(), page_size=2)
    assert [r["courseId"] for r in records] == [0, 1, 2]


@responses.activate
def test_reponse_html_au_lieu_de_json(monkeypatch) -> None:
    """Recevoir du HTML signale une session expirée : message dédié."""
    monkeypatch.setattr(velib, "RETRY_BACKOFF", 0)
    responses.add(
        responses.GET, velib.COURSE_LIST_URL, body="<html>connexion</html>", status=200
    )
    with pytest.raises(velib.VelibError, match="non JSON"):
        velib.fetch_courses(velib.build_session())


@responses.activate
def test_trajets_tries_du_plus_ancien_au_plus_recent(monkeypatch) -> None:
    """`get_new_velib_trips` retourne un historique chronologique et filtré."""
    monkeypatch.setattr(velib, "PAGE_DELAY", 0)
    _enregistrer_amorcage()
    responses.add(
        responses.GET, velib.LOGIN_URL,
        body='<input name="_csrf_token" value="jeton"/>', status=200,
    )
    responses.add(responses.POST, velib.LOGIN_URL, status=302,
                  headers={"Location": velib.ACCOUNT_URL})
    responses.add(responses.GET, velib.ACCOUNT_URL, body="ok", status=200)
    responses.add(
        responses.GET, velib.COURSE_LIST_URL, status=200,
        json={
            "actionStatus": {"status": "SUCCESS"},
            "paging": {"totalNumberOfRecords": 3},
            "walletOperations": [
                {"courseId": 2, "startDate": "2026-09-05T10:00:00Z", "quantity": 5},
                {"courseId": 1, "startDate": "2026-09-05T08:00:00Z", "quantity": 5},
                {"courseId": 3, "startDate": "2026-09-05T09:00:00Z", "quantity": 0},  # écarté
            ],
        },
    )

    trips = velib.get_new_velib_trips("a@b.fr", "motdepasse")
    assert [t.trip_id for t in trips] == ["1", "2"]


# --------------------------------------------------------------------------- #
# Blocages par réputation d'IP
# --------------------------------------------------------------------------- #

@responses.activate
def test_blocage_par_ip_detecte() -> None:
    """La page de blocage cite les deux signaux du Threat Score.

    Cloudflare additionne la réputation de l'IP appelante et l'empreinte
    TLS/HTTP du client. On ne peut rien à la première sur un runner hébergé, et
    tout à la seconde : le message doit donc nommer les deux et proposer la
    parade praticable.
    """
    responses.add(
        responses.GET, velib.LOGIN_URL,
        body="<html><title>Site not reachable</title></html>", status=403,
    )
    with pytest.raises(velib.CloudflareChallenge, match="réputation"):
        velib._request(velib.build_session(), "GET", velib.LOGIN_URL)


@responses.activate
def test_403_sans_marqueur_evoque_brotli() -> None:
    """Un corps illisible signale généralement l'absence du paquet Brotli."""
    responses.add(responses.GET, velib.LOGIN_URL, body="\x1f\x8b\x08 binaire", status=403)
    with pytest.raises(velib.CloudflareChallenge, match="Brotli"):
        velib._request(velib.build_session(), "GET", velib.LOGIN_URL)


@responses.activate
def test_un_403_est_vide_integralement_dans_les_journaux(caplog) -> None:
    """Un blocage doit être diagnosticable à la seule lecture des journaux.

    C'est toute la raison d'être du vidage : le HTTP 403 ne se produit que sur
    le runner, où l'on ne peut ni rejouer la requête ni attacher un débogueur.
    En-têtes complets, code d'erreur WAF et corps brut doivent donc apparaître.
    """
    responses.add(
        responses.GET, velib.LOGIN_URL, status=403,
        body='<html><span class="cf-error-code">1020</span>'
             "<h1>Sorry, you have been blocked</h1></html>",
        headers={"Server": "cloudflare", "Cf-Ray": "9f00ba12cd34ef56-CDG"},
    )
    with caplog.at_level("ERROR"):
        with pytest.raises(velib.CloudflareChallenge) as echec:
            velib._request(velib.build_session(), "GET", velib.LOGIN_URL)

    assert "DIAGNOSTIC HTTP" in caplog.text
    assert "server: cloudflare" in caplog.text.lower()
    assert "9f00ba12cd34ef56-CDG" in caplog.text
    assert "Sorry, you have been blocked" in caplog.text
    # Le code WAF remonte aussi dans le message d'exception : c'est lui qui
    # apparaît dans le résumé d'échec du workflow, pas le corps de la page.
    assert "1020" in str(echec.value)


@responses.activate
def test_limitation_de_debit_reconnue_et_distinguee(caplog) -> None:
    """Un HTTP 429 est une limitation de débit, pas un problème d'empreinte.

    Mesuré en conditions réelles le 8 septembre 2026 : `/login` renvoie 429
    avec `Retry-After: 86145` et la MÊME page « Site not reachable » qu'un 403.
    Seul le code HTTP les distingue, et la parade est opposée — attendre, plutôt
    que changer d'empreinte ou de runner. Sans ce cas, le 429 passait pour une
    réponse normale : il est inférieur à 500, donc `_request` le retournait tel
    quel.
    """
    responses.add(
        responses.GET, velib.LOGIN_URL, status=429,
        body="<html><title>Site not reachable</title></html>",
        headers={"Server": "cloudflare", "Retry-After": "86145"},
    )
    with caplog.at_level("ERROR"):
        with pytest.raises(velib.CloudflareChallenge) as echec:
            velib._request(velib.build_session(), "GET", velib.LOGIN_URL)

    message = str(echec.value)
    assert "429" in message
    assert "23,9 h" in message  # virgule décimale : message lu par un humain
    assert "PAS un" in message  # ne pas envoyer le lecteur sur une fausse piste
    assert "DIAGNOSTIC HTTP" in caplog.text


@pytest.mark.parametrize(
    ("entete", "attendu"),
    [
        ("86145", "23,9 h"),
        ("120", "120 s"),
        ("Wed, 09 Sep 2026 00:00:00 GMT", "Wed, 09 Sep 2026"),
        (None, ""),
    ],
)
def test_mise_en_mots_du_retry_after(entete, attendu) -> None:
    """`Retry-After` est traduit en durée lisible, y compris sous forme de date."""
    assert attendu in velib._format_retry_after(entete)


@responses.activate
def test_le_conseil_depend_du_moteur_employe(monkeypatch) -> None:
    """Sous `requests`, le message doit orienter vers curl-impersonate.

    Sans cette indication, le lecteur des journaux constate un 403 sans savoir
    quel levier actionner.
    """
    monkeypatch.setattr(http_client, "_curl_module", lambda: object())
    responses.add(
        responses.GET, velib.LOGIN_URL, status=403,
        body="<html>Sorry, you have been blocked</html>",
        headers={"Server": "cloudflare"},
    )
    with pytest.raises(velib.CloudflareChallenge, match="VELIB_HTTP_BACKEND=curl"):
        velib._request(velib.build_session("requests"), "GET", velib.LOGIN_URL)


@responses.activate
def test_debogage_vide_meme_les_reponses_reussies(caplog, monkeypatch) -> None:
    """`VELIB_HTTP_DEBUG=1` sert à comparer une exécution locale et une exécution CI.

    Le vidage passe alors en INFO : il devient un journal d'audit, non plus un
    rapport d'échec.
    """
    monkeypatch.setenv("VELIB_HTTP_DEBUG", "1")
    responses.add(responses.GET, velib.LOGIN_URL, body="<html>ok</html>", status=200,
                  headers={"Server": "cloudflare"})
    with caplog.at_level("INFO"):
        velib._request(velib.build_session(), "GET", velib.LOGIN_URL)
    assert "DIAGNOSTIC HTTP" in caplog.text
    assert "HTTP 200" in caplog.text


# --------------------------------------------------------------------------- #
# Schéma réel, tel qu'observé sur la capture HAR du 5 septembre 2026
# --------------------------------------------------------------------------- #

#: Enregistrement réel, réduit à ses champs utiles. Les identifiants de station
#: sont ceux, internes, que publie l'open data Smovengo.
ENREGISTREMENT_REEL = {
    "id": 474404085,
    "status": "TREATED",
    "parameter2": "TRAJET_USAGE",
    "ratingUnitDescription": "second",
    "quantity": 1593,
    "quantityStr": "27min",
    "startDate": "2026-09-04T03:34:17Z",
    "endDate": "2026-09-04T04:00:53Z",
    "operationDate": "2026-09-04T03:34:17Z",
    "parameter3": {
        "BIKEID": "22167",
        "DISTANCE": "3084.0",
        "usageId": "21715596158",
        "departureStationId": "100910836",
        "arrivalStationId": "52456",
        "AVERAGE_SPEED": 7,
    },
}


def test_stations_lues_dans_parameter3() -> None:
    """Les identifiants de station vivent dans `parameter3`, pas à la racine.

    Les chercher à la racine ne renvoie rien : tous les trajets seraient alors
    écartés comme « station inconnue ».
    """
    trip = velib.normalise_trip(ENREGISTREMENT_REEL)
    assert trip.departure_station_id == "100910836"
    assert trip.arrival_station_id == "52456"


def test_distance_lue_dans_parameter3() -> None:
    """`DISTANCE` est une chaîne (« 3084.0 ») : elle doit être convertie."""
    assert velib.normalise_trip(ENREGISTREMENT_REEL).distance_meters == 3084.0


def test_statut_treated_accepte() -> None:
    """« TREATED » est le statut des trajets réellement facturés."""
    assert velib.normalise_trip(ENREGISTREMENT_REEL) is not None


def test_identifiant_stable_du_trajet() -> None:
    """`usageId` identifie la course elle-même, pas l'opération de facturation.

    Une régularisation de facturation crée une nouvelle opération — donc un
    nouvel `id` — pour la même course ; `usageId`, lui, ne change pas. C'est
    donc la meilleure clé de déduplication.
    """
    assert velib.normalise_trip(ENREGISTREMENT_REEL).trip_id == "21715596158"


def test_parameter3_serialise_en_chaine() -> None:
    """Certaines API Symfony sérialisent `parameter3` en chaîne JSON."""
    import json as _json

    record = dict(ENREGISTREMENT_REEL)
    record["parameter3"] = _json.dumps(ENREGISTREMENT_REEL["parameter3"])
    assert velib.normalise_trip(record).departure_station_id == "100910836"


def test_parameter3_absent_ne_plante_pas() -> None:
    """Un enregistrement sans `parameter3` reste analysable, sans stations."""
    record = {k: v for k, v in ENREGISTREMENT_REEL.items() if k != "parameter3"}
    trip = velib.normalise_trip(record)
    assert trip is not None and trip.departure_station_id is None


def test_annulation_reelle_du_har() -> None:
    """Le trajet 474047244 de la capture : 10 s, même station, 0 m parcouru."""
    record = {
        "id": 474047244, "status": "TREATED", "quantity": 6,
        "startDate": "2026-09-04T06:12:00Z", "endDate": "2026-09-04T06:12:10Z",
        "parameter3": {"departureStationId": "52456", "arrivalStationId": "52456",
                       "DISTANCE": "0.0", "usageId": "21707280423"},
    }
    trip = velib.normalise_trip(record)
    assert trip is not None                      # la normalisation le conserve
    assert trip.is_probable_cancellation is True  # la sélection l'écartera
