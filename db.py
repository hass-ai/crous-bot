# ============================================================
#  DB — SQLite : abonnés, accès payant, logements connus, villes
#  Chaque fonction ouvre/ferme sa connexion : simple et sûr
#  même appelé depuis plusieurs tâches asyncio.
# ============================================================

import sqlite3
from datetime import datetime, timedelta

import config
import parser


def _conn():
    return sqlite3.connect(config.DB_FILE)


def _now():
    return datetime.now().isoformat(timespec="seconds")


def init_db():
    """Crée les tables si besoin. À appeler une fois au démarrage."""
    with _conn() as c:
        # WAL : les lectures (ex. /status pendant qu'un client s'inscrit)
        # ne sont plus bloquées par une écriture en cours (le scan qui
        # réécrit villes_vues/cps_vus toutes les 2 min, une inscription...).
        # Réglage persistant (stocké dans le fichier), mais sans risque à
        # re-exécuter à chaque démarrage.
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id        TEXT PRIMARY KEY,
                ville          TEXT NOT NULL,   -- telle que tapée ("Aix-en-Provence")
                ville_norm     TEXT NOT NULL,   -- normalisée ("aix-en-provence")
                boursier       INTEGER NOT NULL DEFAULT 0,
                inscrit_le     TEXT NOT NULL,
                expire_le      TEXT,            -- NULL = pas d'accès actif
                rappel_envoye  INTEGER NOT NULL DEFAULT 0,
                cp_filter      TEXT,            -- NULL = toute la ville ;
                                                -- "75019" = cet arrondissement
                prenom         TEXT,            -- first_name Telegram
                pseudo         TEXT             -- username Telegram (sans @)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS codes (
                code        TEXT PRIMARY KEY,   -- "CROUS-K7X2-M9P4"
                jours       INTEGER NOT NULL,   -- durée d'accès accordée
                cree_le     TEXT NOT NULL,
                utilise_par TEXT,               -- chat_id, NULL = pas encore utilisé
                utilise_le  TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS essais (
                chat_id  TEXT PRIMARY KEY,      -- a déjà consommé son essai gratuit
                date     TEXT NOT NULL          -- (conservé même après /stop)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                cle     TEXT PRIMARY KEY,       -- réglages admin persistants
                valeur  TEXT NOT NULL           -- ("trial_enabled", "notif_inscriptions")
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS known_listings (
                listing_id  TEXT NOT NULL,
                source      TEXT NOT NULL,      -- 'non-boursier' ou 'boursier'
                vu_le       TEXT NOT NULL,      -- dernier scan où l'état a changé
                disponible  INTEGER NOT NULL DEFAULT 1,  -- état au dernier scan
                PRIMARY KEY (listing_id, source)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS candidatures (
                chat_id         TEXT PRIMARY KEY,  -- une candidature à la fois
                listing_id      TEXT NOT NULL,      -- 'groupe' si notif groupée
                dit_oui_le      TEXT NOT NULL,      -- horodatage du "oui" initial
                relance_envoyee INTEGER NOT NULL DEFAULT 0,
                resolu          INTEGER NOT NULL DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS paiements_en_attente (
                chat_id     TEXT PRIMARY KEY,  -- "J'ai payé" tapé, en attente
                demande_le  TEXT NOT NULL      -- de la réponse admin Reçu/Pas reçu
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS changements_en_attente (
                chat_id     TEXT PRIMARY KEY,  -- demande de /changer, en attente
                zones_json  TEXT NOT NULL,     -- nouvelles zones demandées
                boursier    INTEGER NOT NULL,
                demande_le  TEXT NOT NULL      -- de la validation admin Oui/Non
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS villes_geocodees (
                nom_recherche  TEXT PRIMARY KEY,  -- saisie normalisée ("bordeau")
                lat            REAL NOT NULL,
                lon            REAL NOT NULL,
                nom_officiel   TEXT NOT NULL,
                dept           TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS historique_nouveautes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id   TEXT NOT NULL,
                source       TEXT NOT NULL,      -- 'non-boursier' ou 'boursier'
                ville_norm   TEXT NOT NULL,
                cp           TEXT,
                dept         TEXT,
                lat          REAL,
                lon          REAL,
                detecte_le   TEXT NOT NULL       -- horodatage de la détection
            )
        """)
        # tables transitoires (reconstruites à chaque scan) : si un ancien
        # schéma existe (sans "source" ou sans "lat"), on les recrée
        for table in ("villes_vues", "cps_vus"):
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
            if cols and ("source" not in cols
                         or (table == "villes_vues" and "lat" not in cols)):
                c.execute(f"DROP TABLE {table}")
        c.execute("""
            CREATE TABLE IF NOT EXISTS villes_vues (
                source      TEXT NOT NULL,      -- 'non-boursier' ou 'boursier'
                ville_norm  TEXT NOT NULL,
                ville       TEXT NOT NULL,      -- version d'affichage
                nb          INTEGER NOT NULL,   -- nb de logements au dernier scan
                dept        TEXT,               -- département ("92", "75"...)
                lat         REAL,               -- centre approx. de la ville
                lon         REAL,               -- (moyenne de ses logements)
                PRIMARY KEY (source, ville_norm)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS cps_vus (
                source  TEXT NOT NULL,          -- comptage par code postal
                cp      TEXT NOT NULL,          -- (pour les filtres arrondissement)
                nb      INTEGER NOT NULL,
                PRIMARY KEY (source, cp)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                chat_id     TEXT NOT NULL,      -- plusieurs zones par abonné
                ville       TEXT NOT NULL,      -- affichage ("Paris 19e")
                ville_norm  TEXT NOT NULL,      -- zone ("paris" / "dept:92")
                cp_filter   TEXT NOT NULL DEFAULT '',  -- '' = pas de filtre
                PRIMARY KEY (chat_id, ville_norm, cp_filter)
            )
        """)
        # migration douce des bases créées avant
        for colonne, ddl in (("expire_le", "TEXT"),
                             ("rappel_envoye", "INTEGER NOT NULL DEFAULT 0"),
                             ("cp_filter", "TEXT"),
                             ("prenom", "TEXT"),
                             ("pseudo", "TEXT"),
                             ("tarif", f"INTEGER NOT NULL DEFAULT {config.TARIF_NORMAL}"),
                             ("changement_autorise", "INTEGER NOT NULL DEFAULT 0"),
                             ("nb_rappels", "INTEGER NOT NULL DEFAULT 1"),
                             ("promo_expire_le", "TEXT"),
                             ("logement_trouve", "INTEGER NOT NULL DEFAULT 0")):
            try:
                c.execute(f"ALTER TABLE users ADD COLUMN {colonne} {ddl}")
            except sqlite3.OperationalError:
                pass  # colonne déjà présente
        # migration diff par état : les IDs déjà connus sont réputés
        # disponibles (DEFAULT 1) -> aucune fausse vague à la mise à jour
        try:
            c.execute("ALTER TABLE known_listings ADD COLUMN "
                      "disponible INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        # renommage de la source « public » -> « non-boursier » (plus parlant)
        for table in ("known_listings", "villes_vues", "cps_vus"):
            c.execute(f"UPDATE OR IGNORE {table} SET source = 'non-boursier' "  # nosec B608 - table vient d'un tuple fixe
                      f"WHERE source = 'public'")
            c.execute(f"DELETE FROM {table} WHERE source = 'public'")  # nosec B608 - table vient d'un tuple fixe
        c.execute("UPDATE OR IGNORE settings SET cle = 'last_scan_non-boursier' "
                  "WHERE cle = 'last_scan_public'")
        c.execute("DELETE FROM settings WHERE cle = 'last_scan_public'")
        # migration mono-ville -> multi-zones : on copie la zone stockée
        # dans users vers subscriptions, puis on la vide (idempotent)
        c.execute(
            "INSERT OR IGNORE INTO subscriptions "
            "SELECT chat_id, ville, ville_norm, COALESCE(cp_filter, '') "
            "FROM users WHERE ville_norm != ''"
        )
        c.execute("UPDATE users SET ville = '', ville_norm = '', cp_filter = NULL "
                  "WHERE ville_norm != ''")


# ---------------- Abonnés ----------------

def add_user(chat_id, boursier, prenom=None, pseudo=None, tarif=config.TARIF_NORMAL):
    """Inscrit un abonné (les zones vont dans set_subscriptions,
    l'accès viendra de l'essai ou d'un code). tarif : prix (€) figé
    pour ce chat_id dès l'inscription (lien de démarrage utilisé),
    jamais réécrit ensuite — même prix à chaque renouvellement."""
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO users "
            "(chat_id, ville, ville_norm, boursier, inscrit_le, prenom, pseudo, tarif) "
            "VALUES (?, '', '', ?, ?, ?, ?, ?)",
            (str(chat_id), int(boursier), _now(), prenom, pseudo, tarif),
        )


def get_tarif(chat_id):
    """Tarif (€) figé de cet abonné depuis son inscription. Valeur par
    défaut si le compte n'existe pas (ex. avant tout /start)."""
    with _conn() as c:
        row = c.execute("SELECT tarif FROM users WHERE chat_id = ?",
                         (str(chat_id),)).fetchone()
    return row[0] if row else config.TARIF_NORMAL


def set_promo(chat_id, tarif, heures):
    """Applique un tarif promo temporaire (ex : offre flash 24h) à CE
    chat_id — écrase le tarif figé jusqu'à expiration (cf. get_promos_expirees,
    revert_promo, appelé automatiquement par main.py). N'affecte que ce
    client précis, pas le tarif normal des autres."""
    fin = (datetime.now() + timedelta(hours=heures)).isoformat(timespec="seconds")
    with _conn() as c:
        c.execute(
            "UPDATE users SET tarif = ?, promo_expire_le = ? WHERE chat_id = ?",
            (tarif, fin, str(chat_id)),
        )


def get_promos_expirees():
    """chat_ids dont la promo temporaire (set_promo) vient d'expirer et
    n'a pas encore été remise au tarif normal -> cible de revert_promo."""
    with _conn() as c:
        rows = c.execute(
            "SELECT chat_id FROM users "
            "WHERE promo_expire_le IS NOT NULL AND promo_expire_le <= ?",
            (_now(),),
        ).fetchall()
    return [r[0] for r in rows]


def revert_promo(chat_id):
    """Remet CE chat_id au tarif normal et efface la promo expirée."""
    with _conn() as c:
        c.execute(
            "UPDATE users SET tarif = ?, promo_expire_le = NULL WHERE chat_id = ?",
            (config.TARIF_NORMAL, str(chat_id)),
        )


def set_nb_rappels(chat_id, n):
    """Règle le nombre de notifications voulues par nouveau logement pour
    ce chat_id (1 = comportement normal, jusqu'à MAX_RAPPELS_NOTIF)."""
    with _conn() as c:
        c.execute("UPDATE users SET nb_rappels = ? WHERE chat_id = ?",
                  (int(n), str(chat_id)))


def get_nb_rappels(chat_id):
    """Nombre de notifications voulues par nouveau logement pour ce
    chat_id. 1 par défaut si le compte n'existe pas ou n'a jamais réglé."""
    with _conn() as c:
        row = c.execute("SELECT nb_rappels FROM users WHERE chat_id = ?",
                         (str(chat_id),)).fetchone()
    return row[0] if row and row[0] else 1


def get_rappels_batch(chat_ids):
    """{chat_id: nb_rappels} pour une liste de chat_ids — UNE requête au
    lieu d'une par abonné, décisif à l'échelle d'une grosse vague de
    notifications (cf. main.py:scan_source)."""
    if not chat_ids:
        return {}
    with _conn() as c:
        placeholders = ",".join("?" * len(chat_ids))
        rows = c.execute(
            f"SELECT chat_id, nb_rappels FROM users WHERE chat_id IN ({placeholders})",  # nosec B608 - uniquement des ? générés, valeurs paramétrées
            tuple(chat_ids),
        ).fetchall()
    return {chat_id: nb for chat_id, nb in rows}


def update_identite(chat_id, prenom, pseudo):
    """Rafraîchit le prénom/pseudo Telegram de l'abonné (ils peuvent
    changer côté Telegram). Sans effet si le chat n'est pas inscrit."""
    with _conn() as c:
        c.execute("UPDATE users SET prenom = ?, pseudo = ? WHERE chat_id = ?",
                  (prenom, pseudo, str(chat_id)))


def get_identite(chat_id):
    """(prenom, pseudo) stockés en base, ou (None, None)."""
    with _conn() as c:
        row = c.execute(
            "SELECT prenom, pseudo FROM users WHERE chat_id = ?",
            (str(chat_id),),
        ).fetchone()
    return row if row else (None, None)


def chat_id_par_pseudo(pseudo):
    """chat_id de l'inscrit ayant ce pseudo Telegram (avec ou sans @),
    ou None. Insensible à la casse — sert à /who et /unsub par @pseudo.
    (un bot ne peut pas résoudre un @username via l'API Telegram, on ne
    peut donc chercher que parmi les inscrits dont le pseudo est stocké)"""
    with _conn() as c:
        row = c.execute(
            "SELECT chat_id FROM users WHERE LOWER(pseudo) = LOWER(?)",
            (pseudo.lstrip("@"),),
        ).fetchone()
    return row[0] if row else None


def update_boursier(chat_id, boursier):
    """Change la vue SANS toucher à l'accès (expire_le conservé)."""
    with _conn() as c:
        c.execute("UPDATE users SET boursier = ? WHERE chat_id = ?",
                  (int(boursier), str(chat_id)))


def set_subscriptions(chat_id, zones):
    """Remplace les zones surveillées de l'abonné.
    zones = [(ville_affichage, ville_norm, cp_filter_ou_None), ...]"""
    with _conn() as c:
        c.execute("DELETE FROM subscriptions WHERE chat_id = ?", (str(chat_id),))
        c.executemany(
            "INSERT OR IGNORE INTO subscriptions VALUES (?, ?, ?, ?)",
            [(str(chat_id), v, vn, cp or "") for v, vn, cp in zones],
        )


def get_subscriptions(chat_id):
    """Les zones de l'abonné : [(ville, ville_norm, cp_filter), ...]
    (cp_filter = '' si pas de filtre)."""
    with _conn() as c:
        return c.execute(
            "SELECT ville, ville_norm, cp_filter FROM subscriptions "
            "WHERE chat_id = ? ORDER BY ville",
            (str(chat_id),),
        ).fetchall()


def remove_user(chat_id):
    with _conn() as c:
        c.execute("DELETE FROM subscriptions WHERE chat_id = ?", (str(chat_id),))
        cur = c.execute("DELETE FROM users WHERE chat_id = ?", (str(chat_id),))
        return cur.rowcount > 0  # True si l'abonné existait


def get_user(chat_id):
    """Retourne (boursier, expire_le) ou None."""
    with _conn() as c:
        return c.execute(
            "SELECT boursier, expire_le FROM users WHERE chat_id = ?",
            (str(chat_id),),
        ).fetchone()


def get_subscribers_batch(zones, boursier):
    """Comme get_subscribers, mais pour PLUSIEURS zones en UNE seule
    requête au lieu d'une par zone — évite N allers-retours SQLite quand
    une vague de notifications touche beaucoup de villes/départements
    différents d'un coup (important à l'échelle de centaines d'abonnés).
    Retourne {zone: [(chat_id, cp_filter), ...]}.
    cp_filter='' -> toute la zone ; "75019" -> cet arrondissement.
    Les admins ont un accès permanent (jamais filtrés par l'expiration).
    Une candidature en cours ne coupe PLUS les notifications : elles
    continuent normalement pendant l'attente de validation CROUS."""
    if not zones:
        return {}
    admins = [str(a) for a in config.ADMIN_CHAT_IDS]
    cond_admin = (" OR u.chat_id IN (%s)" % ",".join("?" * len(admins))
                  if admins else "")
    placeholders = ",".join("?" * len(zones))
    with _conn() as c:
        rows = c.execute(
            "SELECT s.ville_norm, s.chat_id, s.cp_filter FROM subscriptions s "  # nosec B608 - uniquement des ? générés, valeurs paramétrées
            "JOIN users u ON u.chat_id = s.chat_id "
            f"WHERE s.ville_norm IN ({placeholders}) AND u.boursier = ? "
            "AND ((u.expire_le IS NOT NULL AND u.expire_le > ?)" + cond_admin + ")",
            (*zones, int(boursier), _now(), *admins),
        ).fetchall()
    resultat = {}
    for zone, chat_id, cp_filter in rows:
        resultat.setdefault(zone, []).append((chat_id, cp_filter))
    return resultat


def get_all_users():
    """Tous les inscrits :
    [(chat_id, boursier, inscrit_le, expire_le, prenom, pseudo), ...]."""
    with _conn() as c:
        return c.execute(
            "SELECT chat_id, boursier, inscrit_le, expire_le, prenom, pseudo "
            "FROM users ORDER BY inscrit_le"
        ).fetchall()


def count_users():
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


# ---------------- Accès (essai gratuit + codes) ----------------

def grant_access(chat_id, jours):
    """Prolonge l'accès de `jours` (depuis maintenant, ou depuis la fin
    de l'accès en cours s'il est encore actif). Retourne la date de fin."""
    with _conn() as c:
        row = c.execute(
            "SELECT expire_le FROM users WHERE chat_id = ?", (str(chat_id),)
        ).fetchone()
        base = datetime.now()
        if row and row[0]:
            actuel = datetime.fromisoformat(row[0])
            base = max(base, actuel)  # accès encore actif -> on prolonge
        fin = base + timedelta(days=jours)
        c.execute(
            "UPDATE users SET expire_le = ?, rappel_envoye = 0 WHERE chat_id = ?",
            (fin.isoformat(timespec="seconds"), str(chat_id)),
        )
    return fin


def essai_deja_utilise(chat_id):
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM essais WHERE chat_id = ?", (str(chat_id),)
        ).fetchone() is not None


def get_essayeurs_non_payeurs(marge_jours=5):
    """Chat_ids actuellement en essai gratuit ACTIF, jamais rechargés
    depuis par un paiement/code (durée d'accès courte, proche d'un essai,
    pas des ~90 jours d'un paiement) : cible de la relance "as-tu reçu une
    notif ?" (cf. bot.py:finessai). marge_jours : tolérance au-dessus de la
    durée d'essai réelle, pour ne JAMAIS inclure quelqu'un qui a payé."""
    with _conn() as c:
        rows = c.execute(
            "SELECT u.chat_id FROM users u "
            "JOIN essais e ON e.chat_id = u.chat_id "
            "WHERE u.expire_le IS NOT NULL AND u.expire_le > ? "
            "AND julianday(u.expire_le) - julianday(u.inscrit_le) <= ?",
            (_now(), marge_jours),
        ).fetchall()
    return [r[0] for r in rows]


def est_en_essai_actif(chat_id, marge_jours=5):
    """True si CE chat_id est actuellement en essai gratuit ACTIF, jamais
    rechargé par un paiement (même logique que get_essayeurs_non_payeurs,
    mais pour un seul chat_id) — utilisé par bot.py:_clavier pour proposer
    le paiement anticipé pendant l'essai."""
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM users u JOIN essais e ON e.chat_id = u.chat_id "
            "WHERE u.chat_id = ? AND u.expire_le IS NOT NULL AND u.expire_le > ? "
            "AND julianday(u.expire_le) - julianday(u.inscrit_le) <= ?",
            (str(chat_id), _now(), marge_jours),
        ).fetchone()
    return row is not None


def terminer_essai(chat_id):
    """Coupe l'accès de ce chat_id immédiatement (fin d'essai forcée)."""
    with _conn() as c:
        c.execute("UPDATE users SET expire_le = ? WHERE chat_id = ?",
                  (_now(), str(chat_id)))


def marquer_essai(chat_id):
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO essais VALUES (?, ?)",
                  (str(chat_id), _now()))


def create_code(code, jours):
    with _conn() as c:
        c.execute("INSERT INTO codes (code, jours, cree_le) VALUES (?, ?, ?)",
                  (code, jours, _now()))


def redeem_code(code, chat_id):
    """Consomme un code s'il est valide et libre ET accorde l'accès,
    dans LA MÊME transaction : un crash ne peut jamais consommer le
    code sans donner les jours. Retourne (jours, date_fin) ou (None, None)."""
    with _conn() as c:
        row = c.execute(
            "SELECT jours FROM codes WHERE code = ? AND utilise_par IS NULL",
            (code,),
        ).fetchone()
        if not row:
            return None, None
        jours = row[0]
        c.execute(
            "UPDATE codes SET utilise_par = ?, utilise_le = ? WHERE code = ?",
            (str(chat_id), _now(), code),
        )
        # prolongation depuis maintenant, ou depuis la fin de l'accès
        # en cours s'il est encore actif (même logique que grant_access)
        actuel = c.execute(
            "SELECT expire_le FROM users WHERE chat_id = ?", (str(chat_id),)
        ).fetchone()
        base = datetime.now()
        if actuel and actuel[0] and datetime.fromisoformat(actuel[0]) > base:
            base = datetime.fromisoformat(actuel[0])
        fin = base + timedelta(days=jours)
        c.execute(
            "UPDATE users SET expire_le = ?, rappel_envoye = 0 WHERE chat_id = ?",
            (fin.isoformat(timespec="seconds"), str(chat_id)),
        )
    return jours, fin


def delete_unused_codes():
    """Invalide tous les codes en circulation (pas encore utilisés).
    Les codes déjà consommés sont conservés (traçabilité qui/quand).
    Retourne le nombre de codes supprimés."""
    with _conn() as c:
        cur = c.execute("DELETE FROM codes WHERE utilise_par IS NULL")
        return cur.rowcount


def users_a_rappeler(seuil_jours=3):
    """Abonnés actifs dont l'accès expire dans moins de `seuil_jours`
    et pas encore prévenus. Retourne [(chat_id, expire_le), ...]."""
    maintenant = _now()
    seuil = (datetime.now() + timedelta(days=seuil_jours)).isoformat(timespec="seconds")
    with _conn() as c:
        return c.execute(
            "SELECT chat_id, expire_le FROM users "
            "WHERE expire_le > ? AND expire_le <= ? AND rappel_envoye = 0",
            (maintenant, seuil),
        ).fetchall()


def marquer_rappel(chat_id):
    with _conn() as c:
        c.execute("UPDATE users SET rappel_envoye = 1 WHERE chat_id = ?",
                  (str(chat_id),))


# ---------------- Stats & broadcast (admin) ----------------

def get_active_chat_ids():
    """Les chat_id inscrits dont l'accès est ACTIF, quelle que soit son
    origine (essai gratuit ou code : les deux posent expire_le). Sert au
    broadcast /annonce. Les admins inscrits sont inclus (accès permanent)."""
    admins = [str(a) for a in config.ADMIN_CHAT_IDS]
    cond_admin = (" OR chat_id IN (%s)" % ",".join("?" * len(admins))
                  if admins else "")
    with _conn() as c:
        rows = c.execute(
            "SELECT chat_id FROM users "  # nosec B608 - uniquement des ? générés, valeurs paramétrées
            "WHERE (expire_le IS NOT NULL AND expire_le > ?)" + cond_admin,
            (_now(), *admins),
        ).fetchall()
    return [r[0] for r in rows]


def get_stats():
    """Chiffres pour /stats : inscrits, actifs, essais, codes, top villes."""
    maintenant = _now()
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        actifs = c.execute(
            "SELECT COUNT(*) FROM users WHERE expire_le > ?", (maintenant,)
        ).fetchone()[0]
        essais = c.execute("SELECT COUNT(*) FROM essais").fetchone()[0]
        codes_libres = c.execute(
            "SELECT COUNT(*) FROM codes WHERE utilise_par IS NULL"
        ).fetchone()[0]
        codes_utilises = c.execute(
            "SELECT COUNT(*) FROM codes WHERE utilise_par IS NOT NULL"
        ).fetchone()[0]
        top_villes_abonnes = c.execute(
            "SELECT s.ville, COUNT(DISTINCT s.chat_id) FROM subscriptions s "
            "JOIN users u ON u.chat_id = s.chat_id WHERE u.expire_le > ? "
            "GROUP BY s.ville_norm ORDER BY COUNT(DISTINCT s.chat_id) DESC LIMIT 3",
            (maintenant,),
        ).fetchall()
    return {
        "total": total,
        "actifs": actifs,
        "expires": total - actifs,
        "essais": essais,
        "codes_libres": codes_libres,
        "codes_utilises": codes_utilises,
        "top_villes": top_villes_abonnes,
    }


# ---------------- Réglages admin (persistants) ----------------

def get_setting(cle, defaut):
    with _conn() as c:
        row = c.execute("SELECT valeur FROM settings WHERE cle = ?", (cle,)).fetchone()
    return row[0] if row else defaut


def set_setting(cle, valeur):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (cle, str(valeur)))


# ---------------- Logements connus (pour le diff) ----------------

def get_known_states(source):
    """L'état T1 (scan précédent) : {listing_id: 1 si disponible, 0 sinon}."""
    with _conn() as c:
        rows = c.execute(
            "SELECT listing_id, disponible FROM known_listings WHERE source = ?",
            (source,),
        ).fetchall()
    return dict(rows)


def save_known_states(etats, source, marquer_absents=True):
    """Enregistre l'état T2 : etats = {listing_id: bool disponible}.
    marquer_absents=True (scan complet) : tout ID connu ABSENT du scan
    passe indisponible — sa future réapparition en disponible sera une
    vraie libération, donc notifiée. À False (scan partiel), on ne
    touche qu'aux IDs scannés (évite les fausses libérations)."""
    now = _now()
    with _conn() as c:
        if marquer_absents:
            c.execute("UPDATE known_listings SET disponible = 0 "
                      "WHERE source = ?", (source,))
        c.executemany(
            "INSERT INTO known_listings (listing_id, source, vu_le, disponible) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(listing_id, source) DO UPDATE SET "
            "disponible = excluded.disponible, vu_le = excluded.vu_le",
            [(i, source, now, int(d)) for i, d in etats.items()],
        )


# ---------------- Villes vues dans les scrapes ----------------

def _source(boursier):
    return "boursier" if boursier else "non-boursier"


def update_villes_vues(compteur, source="non-boursier"):
    """compteur = {ville_norm: (ville, nb, dept, lat, lon)} du dernier scan."""
    with _conn() as c:
        c.execute("DELETE FROM villes_vues WHERE source = ?", (source,))
        c.executemany(
            "INSERT INTO villes_vues VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(source, norm, ville, nb, dept, lat, lon)
             for norm, (ville, nb, dept, lat, lon) in compteur.items()],
        )


def villes_proches(lat, lon, km, boursier=False, exclure=None):
    """Les villes avec logements à moins de `km` du point donné :
    [(ville, ville_norm, nb, distance_km), ...] triées par nb décroissant.
    Sert au choix « ville + villes précises des alentours »."""
    src = _source(boursier)
    with _conn() as c:
        rows = c.execute(
            "SELECT ville, ville_norm, nb, lat, lon FROM villes_vues "
            "WHERE source = ? AND lat IS NOT NULL", (src,)).fetchall()
        if not rows and src == "boursier":  # repli sans cookies
            rows = c.execute(
                "SELECT ville, ville_norm, nb, lat, lon FROM villes_vues "
                "WHERE source = 'non-boursier' AND lat IS NOT NULL").fetchall()
    proches = []
    for ville, norm, nb, vlat, vlon in rows:
        if norm == exclure:
            continue
        d = parser.dist_km(lat, lon, vlat, vlon)
        if d <= km:
            proches.append((ville, norm, nb, round(d)))
    return sorted(proches, key=lambda x: -x[2])


def ville_plus_proche(lat, lon, exclure=None, boursier=False):
    """La ville à CROUS la plus proche du point (hors `exclure`), quelle
    que soit sa distance : (ville, distance_km) ou None."""
    src = _source(boursier)
    with _conn() as c:
        rows = c.execute(
            "SELECT ville, ville_norm, lat, lon FROM villes_vues "
            "WHERE source = ? AND lat IS NOT NULL", (src,)).fetchall()
        if not rows and src == "boursier":
            rows = c.execute(
                "SELECT ville, ville_norm, lat, lon FROM villes_vues "
                "WHERE source = 'non-boursier' AND lat IS NOT NULL").fetchall()
    meilleures = [(ville, parser.dist_km(lat, lon, vlat, vlon))
                  for ville, norm, vlat, vlon in rows if norm != exclure]
    return min(meilleures, key=lambda x: x[1]) if meilleures else None


def nb_logements_rayon(lat, lon, km, boursier=False):
    """Nombre de logements dans un rayon (approx. par centre de ville)."""
    total = sum(nb for _, _, nb, _ in villes_proches(lat, lon, km, boursier))
    return total


def get_radius_subscribers(boursier):
    """Les abonnés ACTIFS en zone rayon : [(chat_id, lat, lon, km), ...].
    Une candidature en cours ne coupe plus les notifications."""
    admins = [str(a) for a in config.ADMIN_CHAT_IDS]
    cond_admin = (" OR u.chat_id IN (%s)" % ",".join("?" * len(admins))
                  if admins else "")
    with _conn() as c:
        rows = c.execute(
            "SELECT s.chat_id, s.ville_norm FROM subscriptions s "  # nosec B608 - uniquement des ? générés, valeurs paramétrées
            "JOIN users u ON u.chat_id = s.chat_id "
            "WHERE s.ville_norm LIKE 'rayon:%' AND u.boursier = ? "
            "AND ((u.expire_le IS NOT NULL AND u.expire_le > ?)" + cond_admin + ")",
            (int(boursier), _now(), *admins),
        ).fetchall()
    resultat = []
    for chat_id, norm in rows:
        r = parser.parse_rayon(norm)
        if r:
            resultat.append((chat_id, *r))
    return resultat


def logger_nouveautes(nouveaux, source):
    """Enregistre chaque logement détecté comme vraiment nouveau, avec sa
    zone — sert de base à /relance pour compter "combien de nouveautés
    ratées" dans le secteur d'un abonné dont l'accès est expiré. Ne peut
    compter qu'à partir du moment où cette fonction tourne (pas d'historique
    rétroactif possible)."""
    if not nouveaux:
        return
    maintenant = _now()
    with _conn() as c:
        c.executemany(
            "INSERT INTO historique_nouveautes "
            "(listing_id, source, ville_norm, cp, dept, lat, lon, detecte_le) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(l["id"], source, l["ville_norm"], l["cp"],
              (l["cp"][:3] if l["cp"] and l["cp"].startswith("97") else
               (l["cp"][:2] if l["cp"] else None)),
              l["lat"], l["lon"], maintenant)
             for l in nouveaux],
        )


def compter_manques(ville_norm, cp_filtre, depuis, source):
    """Nombre de nouveautés enregistrées pour une zone simple
    (ville, département, ou arrondissement précis) depuis une date."""
    with _conn() as c:
        if cp_filtre:
            row = c.execute(
                "SELECT COUNT(*) FROM historique_nouveautes "
                "WHERE source = ? AND cp = ? AND detecte_le > ?",
                (source, cp_filtre, depuis),
            ).fetchone()
        elif ville_norm.startswith("dept:"):
            row = c.execute(
                "SELECT COUNT(*) FROM historique_nouveautes "
                "WHERE source = ? AND dept = ? AND detecte_le > ?",
                (source, ville_norm[5:], depuis),
            ).fetchone()
        else:
            row = c.execute(
                "SELECT COUNT(*) FROM historique_nouveautes "
                "WHERE source = ? AND ville_norm = ? AND detecte_le > ?",
                (source, ville_norm, depuis),
            ).fetchone()
    return row[0] if row else 0


def compter_manques_rayon(lat, lon, km, depuis, source):
    """Nombre de nouveautés enregistrées dans un rayon GPS depuis une
    date (distance calculée en Python, comme get_radius_subscribers)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT lat, lon FROM historique_nouveautes "
            "WHERE source = ? AND detecte_le > ? AND lat IS NOT NULL",
            (source, depuis),
        ).fetchall()
    return sum(1 for r_lat, r_lon in rows
               if parser.dist_km(lat, lon, r_lat, r_lon) <= km)


def get_utilisateurs_expires():
    """[(chat_id, expire_le, inscrit_le, boursier), ...] des inscrits dont
    l'accès n'est PAS actif (expiré ou jamais activé) -> cible de /relance
    et /promo. Exclut TOUJOURS les admins : leur expire_le est souvent NULL
    (accès permanent, jamais besoin d'une date) mais ça ne veut pas dire
    "expiré" — sans cette exclusion, /relance s'enverrait à eux-mêmes.
    Exclut aussi ceux qui ont trouvé leur logement (logement_trouve=1,
    cf. terminer_acces) : inutile de les relancer au paiement, ils n'ont
    plus besoin du bot."""
    admins = [str(a) for a in config.ADMIN_CHAT_IDS]
    placeholders = ",".join("?" * len(admins)) if admins else "''"
    with _conn() as c:
        return c.execute(
            "SELECT chat_id, expire_le, inscrit_le, boursier FROM users "  # nosec B608 - uniquement des ? générés, valeurs paramétrées
            "WHERE (expire_le IS NULL OR expire_le <= ?) "
            "AND logement_trouve = 0 "
            f"AND chat_id NOT IN ({placeholders})",
            (_now(), *admins),
        ).fetchall()


def ville_dept(ville_norm):
    """Département d'une ville vue dans les scans ("92"), None si inconnue."""
    with _conn() as c:
        row = c.execute(
            "SELECT dept FROM villes_vues WHERE ville_norm = ? LIMIT 1",
            (ville_norm,),
        ).fetchone()
    return row[0] if row else None


def update_cps_vus(compteur, source="non-boursier"):
    """compteur = dict {cp: nb} du dernier scan."""
    with _conn() as c:
        c.execute("DELETE FROM cps_vus WHERE source = ?", (source,))
        c.executemany("INSERT INTO cps_vus VALUES (?, ?, ?)",
                      [(source, cp, nb) for cp, nb in compteur.items()])


def _nb_avec_repli(c, requete, source, params):
    """Compte pour la source demandée ; si la vue boursier n'a pas de
    données (pas de cookies), on retombe sur la vue non-boursier."""
    row = c.execute(requete, (source, *params)).fetchone()
    if (row is None or row[0] in (None, 0)) and source == "boursier":
        row = c.execute(requete, ("non-boursier", *params)).fetchone()
    return row[0] if row and row[0] else 0


def nb_logements_cp(cp, boursier=False):
    """Nombre de logements de ce code postal au dernier scan (0 si aucun)."""
    with _conn() as c:
        return _nb_avec_repli(
            c, "SELECT nb FROM cps_vus WHERE source = ? AND cp = ?",
            _source(boursier), (cp,))


def nb_logements_dept(prefixe, boursier=False):
    """Nombre de logements dont le CP commence par ce préfixe ("92" -> 92xxx)."""
    with _conn() as c:
        return _nb_avec_repli(
            c, "SELECT COALESCE(SUM(nb), 0) FROM cps_vus "
               "WHERE source = ? AND cp LIKE ?",
            _source(boursier), (prefixe + "%",))


def nb_logements_ville(ville_norm, boursier=False):
    """Nombre de logements de cette ville au dernier scan (0 si aucun)."""
    with _conn() as c:
        return _nb_avec_repli(
            c, "SELECT nb FROM villes_vues WHERE source = ? AND ville_norm = ?",
            _source(boursier), (ville_norm,))


def ville_connue(ville_norm):
    """True si cette ville est apparue dans le dernier scan (toute vue)."""
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM villes_vues WHERE ville_norm = ?", (ville_norm,)
        ).fetchone() is not None


def top_villes(limit=25, boursier=False):
    """Les villes avec le plus de logements pour /villes, selon la vue
    de l'abonné (repli boursier -> public si pas de cookies)."""
    src = _source(boursier)
    with _conn() as c:
        rows = c.execute(
            "SELECT ville, nb FROM villes_vues "
            "WHERE source = ? AND nb > 0 ORDER BY nb DESC LIMIT ?",
            (src, limit),
        ).fetchall()
        if not rows and src == "boursier":
            rows = c.execute(
                "SELECT ville, nb FROM villes_vues "
                "WHERE source = 'non-boursier' AND nb > 0 ORDER BY nb DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return rows


# ---------------- Candidatures ("tu l'as eue ?") ----------------

def a_candidature_en_cours(chat_id):
    """True si cet abonné a déjà répondu 'oui' à une notif et attend la
    validation CROUS (relance prévue le lendemain) — ses notifications
    continuent normalement pendant l'attente."""
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM candidatures WHERE chat_id = ? AND resolu = 0",
            (str(chat_id),),
        ).fetchone() is not None


def creer_candidature(chat_id, listing_id):
    """Enregistre le 'oui'. Appelé seulement après avoir vérifié qu'il n'y
    a pas de candidature EN COURS (resolu=0) pour ce chat_id — donc toute
    ligne restante ici est une ancienne candidature déjà résolue.
    INSERT OR REPLACE (pas OR IGNORE) : sinon cette ancienne ligne bloque
    silencieusement toute candidature future du même abonné (chat_id est
    la clé primaire), et son 'oui' suivant ne serait jamais enregistré."""
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO candidatures (chat_id, listing_id, dit_oui_le) "
            "VALUES (?, ?, ?)",
            (str(chat_id), listing_id, _now()),
        )


def resoudre_candidature(chat_id):
    """Ferme le dossier (validé ou refusé) : débloque les notifications."""
    with _conn() as c:
        c.execute("UPDATE candidatures SET resolu = 1 WHERE chat_id = ?",
                  (str(chat_id),))


def marquer_relance_envoyee(chat_id):
    with _conn() as c:
        c.execute("UPDATE candidatures SET relance_envoyee = 1 WHERE chat_id = ?",
                  (str(chat_id),))


def candidatures_a_relancer(heure=14):
    """Candidatures dont la relance ('tu l'as eu ?') doit partir
    maintenant : lendemain du 'oui' initial, à partir de `heure`h.
    Retourne [(chat_id, listing_id), ...]."""
    with _conn() as c:
        rows = c.execute(
            "SELECT chat_id, listing_id, dit_oui_le FROM candidatures "
            "WHERE resolu = 0 AND relance_envoyee = 0"
        ).fetchall()
    maintenant = datetime.now()
    resultat = []
    for chat_id, listing_id, dit_oui_le in rows:
        cible = (datetime.fromisoformat(dit_oui_le) + timedelta(days=1)).replace(
            hour=heure, minute=0, second=0, microsecond=0)
        if maintenant >= cible:
            resultat.append((chat_id, listing_id))
    return resultat


def terminer_acces(chat_id):
    """Le logement a été validé par le CROUS : l'abonnement s'arrête ici.
    logement_trouve=1 distingue cette fin d'accès d'une simple expiration
    faute de renouvellement -> exclut ce chat_id de /relance et /promo
    (cf. get_utilisateurs_expires), inutile de le relancer au paiement."""
    with _conn() as c:
        c.execute(
            "UPDATE users SET expire_le = ?, logement_trouve = 1 WHERE chat_id = ?",
            (_now(), str(chat_id)),
        )


# ---------------- Paiement en attente ("J'ai payé") ----------------
# Persisté en base (pas un set() en mémoire) : un redémarrage du bot ne
# doit jamais bloquer une confirmation admin encore en attente.

def demander_verification_paiement(chat_id):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO paiements_en_attente (chat_id, demande_le) "
            "VALUES (?, ?)",
            (str(chat_id), _now()),
        )


def paiement_en_attente(chat_id):
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM paiements_en_attente WHERE chat_id = ?",
            (str(chat_id),),
        ).fetchone() is not None


def resoudre_paiement(chat_id):
    """Retourne True si une demande était bien en attente (et la ferme),
    False si déjà traitée -> évite le double crédit entre deux admins."""
    with _conn() as c:
        cur = c.execute("DELETE FROM paiements_en_attente WHERE chat_id = ?",
                        (str(chat_id),))
        return cur.rowcount > 0


# ---------------- Cache de géocodage ----------------
# Beaucoup d'abonnés tapent les mêmes villes (Paris, Lyon, Nantes...) —
# ce cache évite de rappeler l'API externe geo.api.gouv.fr à chaque fois,
# important lors d'une vague d'inscriptions (après une pub, par exemple).

def get_ville_geocodee(nom_recherche):
    """(lat, lon, nom_officiel, dept) déjà en cache pour cette saisie
    (normalisée), ou None si jamais recherchée."""
    with _conn() as c:
        return c.execute(
            "SELECT lat, lon, nom_officiel, dept FROM villes_geocodees "
            "WHERE nom_recherche = ?",
            (nom_recherche.strip().lower(),),
        ).fetchone()


def save_ville_geocodee(nom_recherche, lat, lon, nom_officiel, dept):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO villes_geocodees VALUES (?, ?, ?, ?, ?)",
            (nom_recherche.strip().lower(), lat, lon, nom_officiel, dept),
        )
