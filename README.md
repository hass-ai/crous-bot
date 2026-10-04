# CROUS Bot — surveillance nationale des logements

Surveillance de `trouverunlogement.lescrous.fr` via son **API JSON**
(toute la France en UNE requête, ~1,5 s), diff par ID, notification
Telegram routée par ville. Multi-utilisateur dès le départ :
l'inscription se fait via `/start` dans Telegram et est prise en compte
**au cycle suivant, sans redémarrer le bot**.

## Fichiers (un rôle chacun)

| Fichier | Rôle |
|---|---|
| `config.py` | tout ce qui se règle : token, tool ID, intervalle de scan |
| `main.py` | point d'entrée — bot Telegram + boucle de scan en parallèle |
| `scraper.py` | appel de l'API de recherche (session anonyme et/ou cookies boursier) |
| `parser.py` | item JSON → logement (id, ville, prix…) + `normalize_city()` |
| `diff.py` | quels IDs sont nouveaux ? (1er run silencieux) |
| `notifier.py` | formatage des messages + envoi avec rate limit (25 msg/s) |
| `bot.py` | commandes Telegram + accès payant (essai 7j, codes, anti-brute-force) |
| `db.py` | SQLite : abonnés, accès/codes, IDs connus, villes vues |

## Commandes

**Client** : `/start` (inscription → essai gratuit 7 j), `/changer`
(ville/boursier sans réinscription), `/activer CODE` (activer ou
prolonger), `/status`, `/villes`, `/stop`, `/aide`.

**Admin** (uniquement les chats listés dans `ADMIN_CHAT_IDS` de `config.py` — les autres
sont ignorés en silence, commandes jamais listées dans /aide) :
- `/gencode [jours]` → code unique `CROUS-XXXX-XXXX` (30 j par défaut ;
  `/gencode 7` = code d'essai à offrir)
- `/try` → active/désactive l'essai gratuit automatique des nouveaux
  inscrits (persistant)
- `/stats` → tableau de bord : inscrits, actifs/expirés, codes, top villes
- `/annonce <texte>` → message à tous les abonnés actifs
- `/mute` / `/unmute` → notifications d'inscription (ON par défaut :
  chaque inscription complétée t'envoie pseudo + chat_id + ville + date)

Anti-brute-force : 5 codes faux **en rafale** (espacés de moins d'une
minute) → le chat est ignoré 5 min (silence total). Des essais espacés
ne comptent pas. Rappel automatique à J-3 avant expiration de l'accès.

## Lancer

```bash
pip install -r requirements.txt
python main.py
```

Optionnel : déposer `Cookies_Hard.JSON` (export navigateur, connecté au
site) à côté de `main.py` pour activer la vue boursier. Sans lui, tout le
monde reçoit la vue publique.

## Comment ça route

1. Un cycle (toutes les 2 min) interroge l'API → ~1400 logements disponibles.
2. Diff par ID contre `data.db` → les nouveaux uniquement.
3. Les nouveaux sont groupés par ville (normalisée : accents, tirets,
   « Cedex », arrondissements gérés).
4. Chaque abonné dont la ville correspond reçoit UN message groupé.
5. Abonnés boursiers ← scrape avec cookies ; non-boursiers ← scrape anonyme.

## Raspberry Pi (systemd)

Voir la section déploiement de `../crous_bot_plan.md`.
