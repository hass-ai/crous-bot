# ============================================================
#  CONFIG — tout ce qui se règle est ici, rien ailleurs
# ============================================================
import os 
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

# Chat_id des ADMINS : seuls ces chats peuvent utiliser les commandes
# admin (mémo complet : /admin) et reçoivent les
# notifications d'inscription, le rapport quotidien et les alertes.
# Les autres sont ignorés en silence total. Chat_id : envoie /id au bot.
ADMIN_CHAT_IDS = ["8122439272", "7669001106"]

# Résidences confirmées FAUSSES par l'admin (vérifié directement auprès du
# CROUS) : leurs nouveaux logements ne sont notifiés qu'aux admins (pour
# garder un œil dessus), jamais aux abonnés normaux. Comparaison sur le nom
# de résidence tel qu'il apparaît dans "titre" (insensible à la casse).
RESIDENCES_SUSPECTES = [
    "RENE MARAN",
]

# Compte Telegram utilisé pour tout contact (code, question, ou souci de
# paiement). Remplace l'ancien Snapchat : le client est DÉJÀ sur Telegram
# quand il lit ça — un clic sur le @ suffit, pas de changement d'app ni de
# demande d'ami à accepter.
TELEGRAM_CONTACT = "@saintcrous"

# Texte du bouton "💬 Contact" (comment te joindre pour un code/paiement)
CONTACT_TEXT = (
    "💬 Pour obtenir un code d'activation ou pour toute question, "
    "contacte-nous :\n"
    f"Telegram : {TELEGRAM_CONTACT}"
)

# Version simplifiée utilisée avec l'offre de paiement direct : pas de
# mention de "code d'activation" (l'accès se fait maintenant par
# validation admin après "✅ J'ai payé", plus par code dans ce flux-là).
CONTACT_TEXT_PAIEMENT = (
    "💬 Une question, un souci avec ton virement ? Contacte-nous :\n"
    f"Telegram : {TELEGRAM_CONTACT}"
)

# Liens de paiement direct (PayPal / Revolut), affichés avec l'offre.
REVOLUT_LINK = "https://revolut.me/aymane016s"
REVOLUT_NAME = "@aymane016s"  # <- À REMPLIR

# La référence (chat_id du client) est envoyée dans un message SÉPARÉ,
# juste après celui-ci (cf. bot.py:offre) — facile à copier/coller seule.
PAIEMENT_TEXT = (
    "💳 <b>Paiement direct</b>\n\n"
    f"• Nom REVOLUT: {REVOLUT_NAME}\n\n"
    f"Pas Revolut ? Écris-nous sur Telegram ({TELEGRAM_CONTACT}) "
    "pour un autre moyen de paiement.\n\n"
    "⚠️ INDIQUE BIEN TA RÉFÉRENCE (message suivant 👇) EN REMARQUE DU "
    "VIREMENT — ça nous permet de retrouver direct ton compte Telegram "
    "et d'accélérer l'activation.\n\n"
    "Une fois le virement fait, appuie sur « ✅ J'ai payé » ci-dessous : "
    "on vérifie et ton accès est activé en quelques minutes, sans plus "
    "attendre."
)

# Tarifs (€) : figés par chat_id dès l'inscription (cf. bot.py:start,
# lien de démarrage utilisé) et jamais réécrits ensuite — même prix à
# chaque renouvellement. TARIF_EARLY réservé aux inscrits via le lien
# "?start=early" (première vague) ; tout le reste (2e vague, nouveaux
# clients après coup) utilise TARIF_NORMAL.
TARIF_NORMAL = 20
TARIF_EARLY = 15

# Texte du bouton "🔎 Trouver mon logement" (l'offre + les tarifs).
# Le contact est ajouté automatiquement à la suite. {tarif} rempli au
# moment de l'envoi avec le tarif figé du chat_id (cf. bot.py:offre).
TARIFS_TEXT = (
    "🏠 <b>Le bot surveille les logements CROUS de TOUTE la France, "
    "24h/24.</b> Dès qu'un logement se libère dans ta ville, tu es "
    "prévenu en quelques secondes — souvent avant tout le monde. En "
    "phase complémentaire, être premier fait toute la différence.\n\n"
    "💰 <b>Tarif</b>\n"
    "• La saison — {tarif} €\n\n"
    "👇 Le paiement direct est juste en dessous."
)

# --- Accès payant ---
# Essai gratuit : DÉSACTIVÉ par défaut. S'active depuis Telegram avec
# /try <jours> (admin). Cette valeur n'est que la durée par défaut.
TRIAL_DAYS = 7
CODE_DEFAULT_DAYS = 30   # durée par défaut d'un code généré par /gencode
PAIEMENT_JOURS = 90      # durée accordée quand un admin confirme "✅ Reçu"
CODE_MAX_ATTEMPTS = 5    # essais de code rapprochés avant blocage...
CODE_ATTEMPT_WINDOW = 60 # ...rapprochés = espacés de moins de X secondes
CODE_COOLDOWN = 5 * 60   # durée du blocage (silence total, en secondes)
RAPPEL_JOURS = 3         # prévenir l'abonné X jours avant l'expiration
MAX_DEPTS = 7            # départements IDF max par abonné (parcours guidé)
MAX_ARRS = 7             # arrondissements parisiens max par abonné
RAYON_KM = 25            # rayon "ville + alentours" (hors IDF)
MAX_PROCHES = 6          # villes voisines max en plus de la ville principale
MAX_RAPPELS_NOTIF = 5    # rappels courts max par nouveau logement (/rappels, self-service)
RELANCE_SEUIL_MANQUES = 5  # /relance : n'envoie qu'à ceux ayant raté au moins ce nombre

# --- Anti-spam /start ---
START_SPAM_MAX = 3       # /start tolérés...
START_SPAM_WINDOW = 60   # ...dans cette fenêtre (secondes)
START_SPAM_MUTE = 30 * 60  # au-delà : silence total sur /start (secondes)

# --- Anti-spam notif admin (offre/paiement) ---
# un même chat_id ne redéclenche la notif "a consulté l'offre" qu'après
# ce délai, pour éviter de spammer l'admin si le client retape sur le bouton
OFFRE_NOTIF_COOLDOWN = 15 * 60

# --- Site CROUS ---
# tool 45 = ancienne campagne (fermée, 0 logement le 07/07/2026).
# tool 47 = phase complémentaire 2026-2027 (vérifié le 07/07/2026 : 562
# logements). Si le site change encore de campagne, il suffit de changer
# ce numéro — mais TOUJOURS vider known_listings pour les deux sources
# juste après (sinon tous les logements de la nouvelle campagne seraient
# vus comme "nouveaux" et déclencheraient une vague de notifications).
TOOL_ID = 47
API_URL = f"https://trouverunlogement.lescrous.fr/api/fr/search/{TOOL_ID}"
PAGE_SIZE = 5000  # assez grand pour tout récupérer en 1 requête (~1400 actuellement)

# base des photos (suivie du champ medias[].src de l'API)
MEDIA_BASE_URL = "https://trouverunlogement.lescrous.fr/media/cache/resolve/preview/"

# --- Notifications avec photo ---
PHOTOS_ENABLED = True   # envoyer la photo du logement avec la notification
PHOTOS_MAX_GROUP = 3    # au-delà de X nouveaux logements d'un coup dans une
                        # ville : message texte groupé (anti-spam), sans photos

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
    "Gecko/20100101 Firefox/140.0"
)

# Cookies de session (export JSON du navigateur) pour la vue "boursier".
# Si le fichier n'existe pas, le bot tourne en vue non-boursier seule.
COOKIES_FILE = "Cookies_Hard.JSON"

# --- Timing ---
# Avec l'API (1 requête par cycle au lieu de 60 pages), on peut scanner
# souvent sans être agressif. 5s = débit moyen ~0,4 req/s, toujours
# modeste, pour une détection quasi immédiate.
SCAN_INTERVAL = 5   # secondes entre deux cycles complets

# Si le cycle précédent a échoué (site en surcharge, "trop nombreux",
# erreur réseau...) : normalement plus vite que SCAN_INTERVAL pour
# attraper la réouverture, mais SCAN_INTERVAL est déjà très rapide (5s)
# donc même valeur ici — pas la peine d'aller plus vite en échec.
SCAN_INTERVAL_RECUPERATION = 5

# En cas d'échec (page "trop nombreux"...), quelques tentatives RAPPROCHÉES
# avant d'abandonner le cycle : la file d'attente du CROUS laisse parfois
# passer une requête sur une courte fenêtre, comme rafraîchir plusieurs
# fois de suite à la main plutôt qu'une seule fois toutes les 30s.
SCAN_BURST_TENTATIVES = 10  # tentatives rapprochées avant d'abandonner
SCAN_BURST_PAUSE = 2        # secondes entre chaque tentative rapprochée

# --- Persistance ---
DB_FILE = "data.db"
# État des conversations /start-/changer en cours (survit à un redémarrage) :
# quelqu'un pile au milieu du parcours d'inscription reprend exactement
# où il en était, au lieu de devoir retaper /start.
PERSISTENCE_FILE = "bot_persistence.pickle"

# --- Debug ---
# Écrit la liste COMPLÈTE des logements scannés dans
# debug_logements_non-boursier.txt / debug_logements_boursier.txt à
# chaque cycle : permet de vérifier ce que le bot voit vraiment.
DEBUG_DUMP = True

# --- Telegram : limite d'envoi (30 msg/s max côté API, on garde de la marge) ---
# Deux files SÉPARÉES (total 29, sous la marge) : les notifs de nouveaux
# logements ne doivent JAMAIS attendre derrière une vague d'inscriptions,
# de paiements ou d'alertes admin qui arriverait en même temps.
# AUTRES baissé à 4 (au lieu de 7) en compensant la hausse de NOTIF à 25,
# pour ne jamais dépasser la vraie limite Telegram même si les deux files
# sont pleines en même temps.
NOTIF_MSG_PER_SECOND = 25   # file prioritaire : notifs de nouveaux logements
AUTRES_MSG_PER_SECOND = 4   # file standard : inscriptions, paiement, admin...

# --- Surveillance de latence (log à chaque cycle + alerte admin si trop lent) ---
SEUIL_LATENCE_REPONSE = 3.0     # secondes : temps de réponse moyen jugé "trop lent"
SEUIL_LATENCE_NOTIF = 1.0       # secondes : temps moyen par notif jugé "trop lent"
LATENCE_ECHANTILLON_MIN = 5     # sous ce nombre de mesures, pas d'alerte (trop peu fiable)
ALERTE_LATENCE_COOLDOWN = 3600  # anti-spam : 1 alerte de latence max par heure (par type)
