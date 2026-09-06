# -*- coding: utf-8 -*-
"""Tests du fichier d'état `processed_trips.json`."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from models import VelibTrip
from state import ProcessedTripsState, time_key


def _trip(trip_id: str = "t1", heure: int = 7) -> VelibTrip:
    return VelibTrip(trip_id, datetime(2026, 9, 5, heure, 0, tzinfo=timezone.utc), 900)


def test_premier_lancement_sans_fichier(tmp_path) -> None:
    """Un fichier absent donne un état vide, pas une erreur."""
    etat = ProcessedTripsState.load(tmp_path / "absent.json")
    assert len(etat) == 0
    assert etat.contains(_trip()) is False


def test_aller_retour_disque(tmp_path) -> None:
    """Un trajet marqué puis rechargé est bien reconnu."""
    chemin = tmp_path / "processed_trips.json"
    etat = ProcessedTripsState.load(chemin)
    etat.mark_processed(_trip(), activity_id=42)
    assert etat.save() is True

    recharge = ProcessedTripsState.load(chemin)
    assert recharge.contains(_trip()) is True


def test_detection_par_la_date_si_l_identifiant_change(tmp_path) -> None:
    """Un identifiant renuméroté par l'API ne doit pas créer de doublon."""
    chemin = tmp_path / "processed_trips.json"
    etat = ProcessedTripsState.load(chemin)
    etat.mark_processed(_trip("ancien-id"))
    etat.save()

    recharge = ProcessedTripsState.load(chemin)
    assert recharge.contains(_trip("nouvel-id")) is True


def test_trajet_a_une_autre_heure_non_confondu(tmp_path) -> None:
    """La clé temporelle ne doit pas capter un trajet réellement différent."""
    etat = ProcessedTripsState.load(tmp_path / "s.json")
    etat.mark_processed(_trip("t1", heure=7))
    assert etat.contains(_trip("t2", heure=8)) is False


def test_pas_d_ecriture_sans_modification(tmp_path) -> None:
    """Un état inchangé ne réécrit pas le fichier : évite un commit vide en CI."""
    chemin = tmp_path / "processed_trips.json"
    etat = ProcessedTripsState.load(chemin)
    assert etat.save() is False
    assert not chemin.exists()


def test_ecriture_forcee(tmp_path) -> None:
    """`force=True` écrit même sans modification."""
    chemin = tmp_path / "processed_trips.json"
    ProcessedTripsState.load(chemin).save(force=True)
    assert chemin.is_file()


def test_fichier_corrompu_ne_bloque_pas(tmp_path, caplog) -> None:
    """Un JSON illisible est signalé mais l'exécution continue."""
    chemin = tmp_path / "processed_trips.json"
    chemin.write_text("{ceci n'est pas du JSON", encoding="utf-8")
    with caplog.at_level("ERROR"):
        etat = ProcessedTripsState.load(chemin)
    assert len(etat) == 0
    assert "illisible" in caplog.text


def test_format_liste_accepte(tmp_path) -> None:
    """Une ancienne liste d'identifiants reste lisible."""
    chemin = tmp_path / "processed_trips.json"
    chemin.write_text(json.dumps(["t1", "t2"]), encoding="utf-8")
    assert ProcessedTripsState.load(chemin).contains(_trip("t1")) is True


def test_format_processed_liste_accepte(tmp_path) -> None:
    """Le format `{"processed": [...]}` est également accepté."""
    chemin = tmp_path / "processed_trips.json"
    chemin.write_text(json.dumps({"processed": ["t1"]}), encoding="utf-8")
    assert ProcessedTripsState.load(chemin).contains(_trip("t1")) is True


def test_metadonnees_enregistrees(tmp_path) -> None:
    """Les métadonnées facilitent le diagnostic a posteriori."""
    chemin = tmp_path / "processed_trips.json"
    etat = ProcessedTripsState.load(chemin)
    etat.mark_processed(_trip("t1"), activity_id=987, note="openrouteservice")
    etat.save()

    entree = json.loads(chemin.read_text(encoding="utf-8"))["processed"]["t1"]
    assert entree["strava_activity_id"] == "987"
    assert entree["note"] == "openrouteservice"
    assert entree["duration_seconds"] == 900


def test_ecriture_atomique(tmp_path) -> None:
    """Aucun fichier temporaire ne doit subsister après une écriture réussie."""
    chemin = tmp_path / "processed_trips.json"
    etat = ProcessedTripsState.load(chemin)
    etat.mark_processed(_trip())
    etat.save()
    assert list(tmp_path.glob("*.tmp")) == []


def test_cle_temporelle_normalisee_en_utc() -> None:
    """Une date naïve et son équivalent UTC donnent la même clé."""
    assert time_key(datetime(2026, 9, 5, 7, 0)) == "2026-09-05T07:00:00Z"
