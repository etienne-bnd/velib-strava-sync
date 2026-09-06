# PROJET : Synchronisation Vélib' Métropole vers Strava

## 🎯 Contexte et Objectif
Tu es un assistant de développement expert en Python. Ton objectif est de construire une intégration complète et automatisée qui récupère l'historique des trajets Vélib' de l'utilisateur, recrée un itinéraire GPS réaliste, et l'envoie sur Strava. 
Ce projet sera hébergé sur GitHub et exécuté quotidiennement via GitHub Actions.

## 🏗️ Architecture du Projet
Tu dois créer une architecture modulaire et maintenable. Voici la structure attendue :

- `velib.py` : Module de scraping/connexion pour récupérer les trajets bruts.
- `routing.py` : Module de géolocalisation et de calcul d'itinéraire.
- `gpx_builder.py` : Module de génération du fichier `.gpx`.
- `strava.py` : Module d'authentification et d'upload vers l'API Strava.
- `main.py` : Script d'orchestration principal.
- `requirements.txt` : Dépendances du projet.
- `.github/workflows/sync.yml` : Workflow GitHub Actions.
- `processed_trips.json` : Fichier d'état (cache) pour éviter d'envoyer les mêmes trajets en double.

---

## 🛠️ Étapes de développement (à exécuter dans cet ordre)

### Étape 1 : Le module Vélib' (`velib.py`)
- L'utilisateur a déjà généré un script d'extraction fiable utilisant `requests` (basé sur l'analyse d'un fichier HAR). 
- **Action attendue :** Demande à l'utilisateur de te fournir le contenu de `velib_history.py` (s'il ne l'a pas déjà fait). Intègre cette logique dans une classe ou une fonction `get_new_velib_trips(username, password)`.
- **Filtre :** Ne retourne que les trajets dont le statut est valide et dont la durée (`quantity`) est supérieure à 0.

### Étape 2 : Le module de Cartographie (`routing.py`)
- **Géolocalisation des stations :** Utilise l'Open Data publique Smovengo (`https://velib-metropole-opendata.smoove.pro/opendata/Velib_Metropole/station_information.json`) pour faire correspondre les `departureStationId` et `arrivalStationId` du json Vélib' avec leurs coordonnées GPS (Latitude/Longitude).
- **Calcul d'itinéraire :** Utilise l'API publique d'**OpenRouteService** (endpoint : `/v2/directions/cycling-regular`).
- **Action attendue :** Crée une fonction `get_route_coordinates(start_coords, end_coords, api_key)` qui retourne la liste des points GPS formant le chemin cyclable entre la station A et la station B.

### Étape 3 : Le module GPX (`gpx_builder.py`)
- **Le défi :** L'API Vélib' donne une heure de départ et une durée totale. L'API OpenRouteService donne des points GPS, mais pas de timestamps.
- **Action attendue :** Utilise la librairie `gpxpy`. Crée une fonction `build_gpx(route_points, start_time, duration_seconds)`.
- **Logique mathématique :** Tu dois répartir le temps (`duration_seconds`) uniformément sur tous les segments de `route_points`. Attribue un timestamp précis (format UTC ISO) à chaque point GPS du tracé pour que Strava reconnaisse une vitesse moyenne constante. Le fichier final doit être sauvegardé temporairement sur le disque (ex: `temp_trip.gpx`).

### Étape 4 : Le module Strava (`strava.py`)
- Utilise l'API Strava et la librairie `stravalib` (ou des appels `requests` directs).
- **Authentification :** Implémente le flux OAuth2 basé sur un Refresh Token. Crée une fonction `get_access_token(client_id, client_secret, refresh_token)` qui appelle `POST https://www.strava.com/api/v3/oauth/token` pour obtenir un token frais.
- **Upload :** Crée une fonction `upload_to_strava(access_token, gpx_file_path, trip_name)` qui poste le fichier GPX via l'endpoint `POST /uploads`. Paramètres requis : `data_type="gpx"`, `activity_type="ride"`, et le fichier en pièce jointe.
Description de l'activité : Lors de l'upload, ajoute systématiquement une description (champ description) à l'activité Strava indiquant qu'elle a été générée automatiquement, avec le lien vers le dépôt GitHub du projet. Par exemple : "🚲 Trajet Vélib' synchronisé automatiquement. Code source : https://github.com/VOTRE_PSEUDO/NOM_DU_PROJET"

### Étape 5 : L'orchestrateur (`main.py`) et la gestion d'état
- C'est le chef d'orchestre. Il doit lier tous les modules.
- **Gestion des doublons :** Avant de traiter un trajet, le script doit lire un fichier local `processed_trips.json`. Si l'ID du trajet Vélib' (ou sa date exacte de départ) s'y trouve, on le passe. Si le trajet est uploadé avec succès sur Strava, on ajoute son identifiant à la liste et on sauvegarde le fichier JSON.
- **Temporisation :** Ajoute des `time.sleep()` entre les requêtes OpenRouteService et Strava pour respecter les rate-limits de ces API gratuites.

### Étape 6 : CI/CD (`.github/workflows/sync.yml`)
- Rédige le workflow GitHub Actions pour exécuter `main.py` tous les jours à 23h30 UTC.
- **Variables secrètes :** Utilise `env` pour injecter : `VELIB_USERNAME`, `VELIB_PASSWORD`, `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REFRESH_TOKEN`, `ORS_API_KEY`.
- **Commit automatique :** Le workflow DOIT comporter une étape finale (utilisant par exemple `stefanzweifel/git-auto-commit-action`) pour commiter et pousser la mise à jour du fichier `processed_trips.json` dans le dépôt afin que l'état soit conservé pour l'exécution du lendemain.

---

## 🛑 Contraintes et Règles de code
1. **Typage et documentation :** Le code Python doit être strictement typé (Type Hints) et documenté (Docstrings).
2. **Gestion des erreurs (Try/Catch) :** Gère gracieusement les cas où l'API OpenRouteService échoue, ou si un trajet Vélib' est une boucle (départ et arrivée à la même station avec durée de 2 minutes = annulation, à ignorer).
3. **Sécurité :** Ne hardcode AUCUN mot de passe ou token. Utilise exclusivement `os.environ.get()`.
    Variables d'environnement locales : Le projet doit utiliser la librairie python-dotenv pour charger les variables en local depuis un fichier .env. Tu devras me créer un fichier .env.example et configurer le .gitignore pour que le fichier .env réel ne soit jamais commité.
4. **Pas de précipitation :** Propose-moi l'architecture et demande-moi de valider. Développe ensuite module par module. Ne génère pas tout le projet dans un seul énorme bloc de code.
