# Vélib' sur Strava

Publie automatiquement les trajets [Vélib'](https://www.velib-metropole.fr)
sur [Strava](https://www.strava.com)

## Mise en route

Tout se passe sur GitHub : **aucune installation locale n'est nécessaire**. Le
workflow installe les dépendances sur le runner à chaque exécution.

1. **Forker ce dépôt** (bouton *Fork*). Le
   workflow doit tourner sur *votre* dépôt, avec *vos* secrets.

2. **Activer les Actions** : onglet *Actions* → « I understand my workflows, go
   ahead and enable them ». GitHub désactive par défaut les workflows d'un
   dépôt forké, cron compris. Sans ce clic, la synchronisation quotidienne ne
   se déclenchera jamais.

3. **Repartir d'un état vierge** : remplacer le contenu de `processed_trips.json`
   par `{"processed": {}}`. Ce fichier mémorise les trajets déjà envoyés par le
   propriétaire du dépôt d'origine ; le garder ferait sauter des trajets.
   En revanche, **conservez `stations_cache.json`** : c'est le référentiel de
   secours des stations, et l'open data Smovengo est régulièrement injoignable.

4. **Renseigner les six secrets** dans *Settings → Secrets and variables →
   Actions* : `VELIB_USERNAME`, `VELIB_PASSWORD`, `STRAVA_CLIENT_ID`,
   `STRAVA_CLIENT_SECRET`, `STRAVA_REFRESH_TOKEN`, `ORS_API_KEY`.
   Voir [Obtenir les identifiants](#obtenir-les-identifiants) cela se fait
   dans un navigateur, sans rien installer.

5. **Premier essai à blanc** : *Actions → Synchronisation Vélib' vers Strava →
   Run workflow*, avec `dry_run: true` et `max_trips: 1`. Rien n'est envoyé sur
   Strava ; les journaux disent si les quatre API répondent.

Ensuite, le workflow tourne seul chaque jour à 23h30 UTC et commite
`processed_trips.json` pour conserver l'état d'une exécution à l'autre.

> Sur un dépôt **public**, GitHub désactive les workflows planifiés après
> 60 jours sans activité du dépôt. Les commits du bot ne relancent pas ce
> compteur : si la synchronisation s'arrête sans raison après deux mois,
> c'est là qu'il faut regarder.

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

## Installation locale (facultative)

Inutile pour l'usage courant : elle ne sert qu'à diagnostiquer une panne, à
modifier le code, ou à rattraper d'un coup un gros arriéré de trajets sans
attendre le plafond quotidien.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # puis renseigner les mêmes valeurs que les secrets
```

```bash
python check_apis.py         # vérifie que les quatre API répondent
python probe_cloudflare.py   # diagnostique le pare-feu, sans identifiants
python main.py --dry-run     # construit les GPX sans rien envoyer
python main.py -v --max-trips 1   # premier envoi prudent, en mode bavard
python main.py               # synchronisation réelle
pytest                       # 242 tests, aucun appel réseau réel
```

### Rattraper tout l'historique

Par défaut, seuls les trajets des 30 derniers jours sont traités, 25 par
exécution. Pour reprendre l'historique complet :

```bash
MAX_TRIP_AGE_DAYS=0 python main.py --dry-run --max-trips 200 -v   # simulation
MAX_TRIP_AGE_DAYS=0 python main.py --max-trips 30 -v              # puis par lots
```

Procédez par lots d'une trentaine espacés d'un quart d'heure : chaque trajet
consomme deux à trois appels Strava, et l'API en autorise cent par tranche de
15 minutes. L'état déduplique, chaque lot reprend où le précédent s'est arrêté.

## Franchir Cloudflare

`velib-metropole.fr` est protégé par Cloudflare Bot Management, qui additionne
plusieurs signaux en un *Threat Score*. Trois d'entre eux concernent ce projet.

**1. Le cookie `__cf_bm`.** Une requête vers `/login` qui arrive sans ce cookie
reçoit un HTTP 403. Un navigateur ne rencontre jamais ce cas : il atteint
`/login` depuis une autre page du site, donc il l'a déjà. `velib.py` visite donc
l'accueil avant `/login` — **ne pas supprimer cette étape**, un test la protège.

**2. L'empreinte TLS et HTTP/2.** `requests` négocie le TLS via OpenSSL et parle
HTTP/1.1 : l'ordre des ciphers, des extensions et des courbes suffit à
l'identifier (JA3/JA4), et aucun navigateur n'ouvre plus une page en HTTP/1.1.
Un jeu d'en-têtes réaliste n'y change rien — le signal est *sous* HTTP. C'est ce
que corrige [`curl_cffi`](https://github.com/lexiforest/curl_cffi), qui embarque
*curl-impersonate* et reproduit l'empreinte d'un Chrome réel. Mesuré depuis ce
projet, sur `/cdn-cgi/trace` :

| Moteur | `http=` | `uag=` |
|---|---|---|
| `requests` | `http/1.1` | Chrome 152 déclaré à la main |
| `curl` | `http/2` | Chrome 150, natif et cohérent avec l'empreinte |

**3. La réputation de l'IP.** Les runners GitHub Actions hébergés partagent des
plages Azure médiocrement notées. On ne peut rien y faire — sinon un *runner
auto-hébergé*. L'empreinte, elle, est entièrement sous notre contrôle, et c'est
la somme des deux qui décide : une IP de centre de données **plus** une empreinte
« Python » franchit le seuil de blocage ; la même IP avec une empreinte Chrome
reste souvent en dessous.

Le workflow pose donc `VELIB_HTTP_BACKEND=curl`, ce qui **exige** `curl_cffi` :
en son absence l'exécution échoue immédiatement, avec un message explicite,
plutôt que de retomber en silence sur `requests` et de rendre un HTTP 403
inexplicable. Aucun conteneur Docker n'est nécessaire — la roue manylinux de
`curl_cffi` embarque le libcurl patché.

### Diagnostiquer un blocage

Toute réponse inattendue est vidée dans les journaux : statut, **intégralité des
en-têtes**, code d'erreur WAF (1020, 1015, 1010…), Ray ID, cookies acquis et
**corps brut**. Les valeurs de cookies sont masquées et aucun corps de requête
n'est journalisé — celui du POST `/login` contient le mot de passe.

```bash
VELIB_HTTP_DEBUG=1 python main.py -v     # vide AUSSI les réponses réussies
python probe_cloudflare.py               # compare les deux moteurs, sans identifiants
```

`probe_cloudflare.py` n'envoie aucun identifiant : il s'arrête à la page publique
`/login`. Il tourne automatiquement dans le workflow après un échec. Sa lecture :

| Observation | Conclusion |
|---|---|
| `curl` passe, `requests` non | L'empreinte TLS était le signal bloquant |
| Les deux échouent, code WAF 1020 | Règle de pare-feu (ASN, pays) : runner auto-hébergé |
| Les deux échouent, code WAF 1015 ou HTTP 429 | Limitation de débit : attendre, espacer les exécutions |
| `/cdn-cgi/trace` échoue lui aussi | Problème réseau, pas Cloudflare |

Si un blocage persiste, le premier réglage à tenter est une autre cible
d'imitation : `VELIB_IMPERSONATE=chrome131`, `firefox`, `safari`.

### Variables d'environnement du transport

| Variable | Défaut | Rôle |
|---|---|---|
| `VELIB_HTTP_BACKEND` | `auto` | `curl` (exige curl_cffi), `requests`, ou `auto` |
| `VELIB_IMPERSONATE` | `chrome` | Navigateur imité |
| `VELIB_HTTP_DEBUG` | `0` | Vide toutes les réponses, pas seulement les échecs |
| `VELIB_HTTP_DUMP_BODY_CHARS` | `4000` | Longueur de corps journalisée |

## Codes de sortie

| Code | Signification |
|---|---|
| 0 | Succès (y compris « aucun nouveau trajet ») |
| 1 | Échec général |
| 2 | Configuration invalide ou incomplète |
| 3 | Blocage anti-bot sur velib-metropole.fr (403, 429 ou challenge) |
