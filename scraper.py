# ============================================================
#  SCRAPER — récupère TOUS les logements via l'API JSON du site
#  (celle qu'utilise le front Svelte). UNE seule requête POST
#  suffit pour toute la France : pas de pagination HTML, pas de
#  dérive d'ordre entre les pages, IDs uniques garantis.
#  Vérifié le 04/07/2026 : 1423/1423 logements en ~2s.
# ============================================================

import json
import os
import time

import requests

import config
import db
import parser as crous_parser

# rectangle couvrant toute la France métropolitaine + Corse
_FRANCE = [{"lon": -9.0, "lat": 51.5}, {"lon": 10.0, "lat": 41.0}]

# contexte global du site : contient un bloc `user` non nul UNIQUEMENT
# si la session est connectée (c'est ce qui fait apparaître le bouton
# « Déconnexion » côté front — vérifié en live le 05/07/2026)
CONTEXT_URL = "https://trouverunlogement.lescrous.fr/api/global/context"


def cookies_valides(session):
    """La session boursier est-elle toujours connectée ?
    True/False, ou None si le site est injoignable (on ne conclut rien)."""
    try:
        r = session.get(CONTEXT_URL, timeout=(5, 20))
        r.raise_for_status()
        return bool(r.json().get("user"))
    except Exception:
        return None


def geocoder_ville(nom):
    """Centre d'une commune via l'API officielle geo.api.gouv.fr (gratuite,
    sans clé). Retourne (lat, lon, nom_officiel, dept) ou None si introuvable.
    Le boost population privilégie la grande ville en cas d'homonymes.
    Le département sert à détecter l'Île-de-France (parcours dédié).

    Passe par un cache en base (villes_geocodees) : beaucoup d'abonnés
    tapent les mêmes villes (Paris, Lyon, Nantes...), pas la peine de
    retaper l'API externe à chaque fois — décisif lors d'une vague
    d'inscriptions. Appelé via asyncio.to_thread, donc l'accès DB ici
    ne bloque jamais la boucle asyncio principale."""
    cache = db.get_ville_geocodee(nom)
    if cache:
        return cache
    try:
        r = requests.get(
            "https://geo.api.gouv.fr/communes",
            params={"nom": nom, "fields": "centre,population,codeDepartement",
                    "boost": "population", "limit": 1},
            timeout=10,
        )
        r.raise_for_status()
        resultats = r.json()
        if not resultats:
            return None
        commune = resultats[0]
        lon, lat = commune["centre"]["coordinates"]
        resultat = (lat, lon, commune["nom"], commune.get("codeDepartement"))
        db.save_ville_geocodee(nom, *resultat)
        return resultat
    except Exception:
        return None  # API en panne = comme introuvable, le bot gère


def build_session(with_cookies=False):
    """
    Session HTTP prête à l'emploi.
    with_cookies=True -> charge Cookies_Hard.JSON (vue boursier).
    Retourne None si les cookies sont demandés mais absents.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Language": "fr-FR,fr;q=0.9",
    })

    if with_cookies:
        if not os.path.exists(config.COOKIES_FILE):
            return None
        with open(config.COOKIES_FILE, "r", encoding="utf-8") as f:
            cookies_list = json.load(f)
        for ck in cookies_list:
            session.cookies.set(ck["name"], ck["value"])

    return session


def _search_body(page=1):
    return {
        "idTool": config.TOOL_ID,
        "need_aggregation": False,
        "page": page,
        "pageSize": config.PAGE_SIZE,
        "sector": None,
        "occupationModes": [],
        "residence": None,
        "precision": None,
        "equipment": [],
        "price": {"min": 0, "max": 10000000},
        "location": _FRANCE,
    }


def fetch_all_listings(session):
    """
    Tous les logements DISPONIBLES de France en une passe.
    Retourne (liste_logements, nb_total_annoncé).

    Fonction SYNCHRONE (requests) : à appeler via asyncio.to_thread
    depuis la boucle principale pour ne pas bloquer le bot Telegram.

    En cas d'échec (page "trop nombreux", JSON invalide, erreur réseau...),
    quelques tentatives RAPPROCHÉES avant d'abandonner : la file d'attente
    du CROUS laisse parfois passer une requête sur une courte fenêtre,
    comme rafraîchir plusieurs fois de suite à la main.
    """
    # (connexion 5s, réponse 20s) : si le site rame, on abandonne vite
    # et on retente au cycle suivant plutôt que de suspendre le bot 60s
    derniere_erreur = None
    for tentative in range(1, config.SCAN_BURST_TENTATIVES + 1):
        try:
            r = session.post(config.API_URL, json=_search_body(page=1), timeout=(5, 20))
            r.raise_for_status()
            results = r.json()["results"]
            break  # succès : on sort de la rafale
        except Exception as e:
            derniere_erreur = e
            if tentative < config.SCAN_BURST_TENTATIVES:
                time.sleep(config.SCAN_BURST_PAUSE)
    else:
        raise derniere_erreur

    total = results["total"]["value"]
    items = list(results["items"])

    # sécurité : si un jour le total dépasse PAGE_SIZE, on pagine
    page = 2
    while len(items) < total and page <= 20:
        r = session.post(config.API_URL, json=_search_body(page=page), timeout=(5, 20))
        r.raise_for_status()
        suite = r.json()["results"]["items"]
        if not suite:
            break
        items.extend(suite)
        page += 1

    # On garde AUSSI les logements complets (available=False) : ils
    # servent à connaître les villes qui ont des résidences (choix des
    # alentours) même quand tout est plein. Le diff et les notifications
    # ne travaillent que sur les disponibles (champ "disponible").
    logements = [crous_parser.parse_api_item(it) for it in items]
    return logements, total
