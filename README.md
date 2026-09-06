# Vélib' sur Strava

Synchronise automatiquement l'historique de trajets [Vélib' Métropole](https://www.velib-metropole.fr)
vers [Strava](https://www.strava.com) : chaque trajet est reconstitué en un
itinéraire cyclable réaliste, horodaté, puis envoyé comme activité.

## Fonctionnement

```
Vélib' (getCourseList)  ──►  trajets bruts
        │
Open data des stations  ──►  coordonnées GPS de départ et d'arrivée
        │
OpenRouteService        ──►  tracé cyclable réel (100 à 300 points)
        │
gpxpy                   ──►  GPX horodaté à vitesse constante
        │
Strava (POST /uploads)  ──►  activité créée
        │
processed_trips.json    ──►  mémoire, pour ne jamais créer de doublon
```

## Installation

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # puis renseigner les valeurs
```

### Obtenir les identifiants

| Variable | Où l'obtenir |
|---|---|
| `VELIB_USERNAME` / `VELIB_PASSWORD` | Le compte utilisé sur velib-metropole.fr |
| `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` | https://www.strava.com/settings/api |
| `STRAVA_REFRESH_TOKEN` | Flux OAuth2 manuel, **scope `activity:write`** — ajouter `activity:read_all` pour que le script vérifie ses envois (voir ci-dessous) |
| `ORS_API_KEY` | https://openrouteservice.org/dev/#/signup (gratuit, 2 000 requêtes/jour) |

Pour le jeton de rafraîchissement Strava, ouvrir dans un navigateur :

```
https://www.strava.com/oauth/authorize?client_id=VOTRE_ID&response_type=code&redirect_uri=http://localhost/exchange_token&approval_prompt=force&scope=activity:write,activity:read_all
```

Autoriser, relever le paramètre `code` de l'URL de retour, puis :

```bash
curl -X POST https://www.strava.com/api/v3/oauth/token \
  -d client_id=VOTRE_ID -d client_secret=VOTRE_SECRET \
  -d code=LE_CODE -d grant_type=authorization_code
```

Le champ `refresh_token` de la réponse est la valeur à conserver.

## Utilisation

```bash
python check_apis.py         # vérifie que les quatre API répondent
python main.py --dry-run     # construit les GPX sans rien envoyer
python main.py               # synchronisation réelle
python main.py -v --max-trips 1   # premier envoi prudent, en mode bavard
```

## Tests

```bash
pytest                       # 196 tests, aucun appel réseau réel
```

## Déploiement

Le workflow `.github/workflows/sync.yml` s'exécute chaque jour à 23h30 UTC et
commite `processed_trips.json` pour conserver l'état entre deux exécutions.

Renseigner les six secrets dans **Settings → Secrets and variables → Actions** :
`VELIB_USERNAME`, `VELIB_PASSWORD`, `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`,
`STRAVA_REFRESH_TOKEN`, `ORS_API_KEY`.

> **Point d'attention.** Cloudflare Bot Management protège `velib-metropole.fr`.
> Une requête vers `/login` dépourvue du cookie `__cf_bm` reçoit un HTTP 403.
> `velib.py` visite donc l'accueil avant `/login` pour établir ce cookie — ne pas
> supprimer cette étape. Si un blocage survient malgré tout, le script sort avec
> le code 3 ; la parade est alors un *runner auto-hébergé*.

## Codes de sortie

| Code | Signification |
|---|---|
| 0 | Succès (y compris « aucun nouveau trajet ») |
| 1 | Échec général |
| 2 | Configuration invalide ou incomplète |
| 3 | Blocage anti-bot sur velib-metropole.fr |
