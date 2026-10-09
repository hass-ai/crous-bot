# ============================================================
#  PARSER — item JSON de l'API -> logement propre
#  + normalize_city(), utilisée PARTOUT où on compare des villes
# ============================================================

import math
import re
import unicodedata

import config


def dist_km(lat1, lon1, lat2, lon2):
    """Distance en km entre deux points GPS (formule de Haversine)."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(a))


def parse_rayon(ville_norm):
    """Zone rayon "rayon:44.8624,-0.5848,25" -> (lat, lon, km), sinon None."""
    if not ville_norm.startswith("rayon:"):
        return None
    try:
        lat, lon, km = ville_norm[6:].split(",")
        return float(lat), float(lon), float(km)
    except ValueError:
        return None


def normalize_city(name):
    """
    "Aix en Provence" / "AIX-EN-PROVENCE" / "aix–en–provence" -> "aix-en-provence"
    "Paris 13e", "Montpellier Cedex 5", "Lyon 07" -> "paris", "montpellier", "lyon"
    Appliquée à la fois aux adresses scrapées ET aux villes tapées par
    les abonnés : les deux côtés passent par la même moulinette.
    """
    s = (name or "").strip().lower()
    # accents retirés
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    # tout séparateur -> espace, puis découpage
    s = re.sub(r"[^a-z0-9]+", " ", s)
    tokens = s.split()
    # on retire cedex + tout ce qui suit — y compris collé ("cedex10") —
    # et les arrondissements en fin ("paris 13", "lyon 07", "marseille 13e")
    for i, t in enumerate(tokens):
        if t.startswith("cedex"):
            tokens = tokens[:i]
            break
    while tokens and re.fullmatch(r"\d+(e|er|eme)?", tokens[-1]):
        tokens.pop()
    return "-".join(tokens)


# villes à arrondissements : préfixe CP + bornes valides
_ARRONDISSEMENTS = {"paris": ("75", 1, 20), "lyon": ("69", 1, 9),
                    "marseille": ("13", 1, 16)}

# départements (pour les abonnements type "92" = tout le 92)
_DEPARTEMENTS = {
    "01": "Ain", "02": "Aisne", "03": "Allier", "04": "Alpes-de-Haute-Provence",
    "05": "Hautes-Alpes", "06": "Alpes-Maritimes", "07": "Ardèche", "08": "Ardennes",
    "09": "Ariège", "10": "Aube", "11": "Aude", "12": "Aveyron",
    "13": "Bouches-du-Rhône", "14": "Calvados", "15": "Cantal", "16": "Charente",
    "17": "Charente-Maritime", "18": "Cher", "19": "Corrèze", "20": "Corse",
    "21": "Côte-d'Or", "22": "Côtes-d'Armor", "23": "Creuse", "24": "Dordogne",
    "25": "Doubs", "26": "Drôme", "27": "Eure", "28": "Eure-et-Loir",
    "29": "Finistère", "30": "Gard", "31": "Haute-Garonne", "32": "Gers",
    "33": "Gironde", "34": "Hérault", "35": "Ille-et-Vilaine", "36": "Indre",
    "37": "Indre-et-Loire", "38": "Isère", "39": "Jura", "40": "Landes",
    "41": "Loir-et-Cher", "42": "Loire", "43": "Haute-Loire", "44": "Loire-Atlantique",
    "45": "Loiret", "46": "Lot", "47": "Lot-et-Garonne", "48": "Lozère",
    "49": "Maine-et-Loire", "50": "Manche", "51": "Marne", "52": "Haute-Marne",
    "53": "Mayenne", "54": "Meurthe-et-Moselle", "55": "Meuse", "56": "Morbihan",
    "57": "Moselle", "58": "Nièvre", "59": "Nord", "60": "Oise",
    "61": "Orne", "62": "Pas-de-Calais", "63": "Puy-de-Dôme", "64": "Pyrénées-Atlantiques",
    "65": "Hautes-Pyrénées", "66": "Pyrénées-Orientales", "67": "Bas-Rhin", "68": "Haut-Rhin",
    "69": "Rhône", "70": "Haute-Saône", "71": "Saône-et-Loire", "72": "Sarthe",
    "73": "Savoie", "74": "Haute-Savoie", "75": "Paris", "76": "Seine-Maritime",
    "77": "Seine-et-Marne", "78": "Yvelines", "79": "Deux-Sèvres", "80": "Somme",
    "81": "Tarn", "82": "Tarn-et-Garonne", "83": "Var", "84": "Vaucluse",
    "85": "Vendée", "86": "Vienne", "87": "Haute-Vienne", "88": "Vosges",
    "89": "Yonne", "90": "Territoire de Belfort", "91": "Essonne", "92": "Hauts-de-Seine",
    "93": "Seine-Saint-Denis", "94": "Val-de-Marne", "95": "Val-d'Oise",
    "971": "Guadeloupe", "972": "Martinique", "973": "Guyane", "974": "La Réunion",
    "976": "Mayotte",
}


def dept_label(prefix):
    """"92" -> "Hauts-de-Seine (92)"."""
    nom = _DEPARTEMENTS.get(prefix)
    return f"{nom} ({prefix})" if nom else f"département {prefix}"


def parse_city_query(text):
    """
    Interprète la ville saisie par un abonné.
    Retourne (ville_norm, cp_filtre, arr_invalide) :
      "paris"           -> ("paris", None, False)      tout Paris
      "paris 19"        -> ("paris", "75019", False)   uniquement le 19e
      "lyon 7e"         -> ("lyon", "69007", False)
      "paris 75019"     -> ("paris", "75019", False)
      "nantes 44200"    -> ("nantes", "44200", False)  CP complet = filtre direct
      "marseille 21"    -> ("marseille", None, True)   arrondissement INEXISTANT
      "toulouse 5"      -> ("toulouse", None, False)   pas d'arrondissements là-bas
    """
    s = (text or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    tokens = re.sub(r"[^a-z0-9]+", " ", s).split()

    # un numéro de département seul : "92" -> tout le 92 ("2a"/"2b" -> Corse)
    if len(tokens) == 1:
        t = {"2a": "20", "2b": "20"}.get(tokens[0], tokens[0])
        if t in _DEPARTEMENTS:
            return f"dept:{t}", None, False

    cp_filtre = None
    arr_invalide = False
    if len(tokens) >= 2:
        dernier = tokens[-1]
        if re.fullmatch(r"\d{5}", dernier):
            cp_filtre = dernier  # CP complet, quelle que soit la ville
        elif re.fullmatch(r"\d{1,2}(e|er|eme)?", dernier):
            base = "-".join(tokens[:-1])
            arr = _ARRONDISSEMENTS.get(base)
            if arr:
                prefixe, mini, maxi = arr
                num = int(re.sub(r"\D", "", dernier))
                if mini <= num <= maxi:
                    cp_filtre = f"{prefixe}{num:03d}"  # paris+19 -> 75019
                else:
                    arr_invalide = True  # "marseille 21" : n'existe pas

    return normalize_city(text), cp_filtre, arr_invalide


def format_zone(ville, cp):
    """Nom lisible d'une zone filtrée : ("paris 19", "75019") -> "Paris 19e",
    ("lyon", "69001") -> "Lyon 1er", ("nantes 44200", "44200") -> "Nantes (44200)".
    Le nom repart de la commune normalisée : la saisie peut déjà contenir
    le numéro ("paris 19"), on ne veut pas "Paris 19 19e"."""
    base = normalize_city(ville)
    nom = " ".join(p.capitalize() for p in base.split("-")) or (ville or "").strip().title()
    if not cp:
        return nom
    arr = _ARRONDISSEMENTS.get(base)
    if arr:
        prefixe, mini, maxi = arr
        if cp.startswith(prefixe) and len(cp) == 5:
            num = int(cp[len(prefixe):])
            if mini <= num <= maxi:
                return f"{nom} {num}{'er' if num == 1 else 'e'}"
    return f"{nom} ({cp})"


# Code postal, avec tolérance pour l'espace interne : "73000" ou "73 000"
_RE_CP = re.compile(r"\b(\d{2}\s?\d{3})\b")


def extract_city(adresse):
    """
    Retourne (code_postal, ville_affichage, ville_norm) depuis l'adresse.
    Cas gérés :
      "75 Av. Fliche 34090 Montpellier Cedex 5" -> ville après le CP (standard)
      "58 Chemin de la Cardinière 73 000 CHAMBERY" -> CP avec espace
      "25 RUE DE L'ARGONNE PARIS 75019"           -> ville AVANT le CP
    """
    adresse = (adresse or "").strip()
    matches = list(_RE_CP.finditer(adresse))
    if not matches:
        return None, None, ""

    m = matches[-1]  # le dernier CP de la chaîne (le premier peut être un n° de rue)
    cp = m.group(1).replace(" ", "")
    apres = adresse[m.end():].strip(" ,-")

    if apres:
        ville_brute = apres
    else:
        # rien après le CP -> la ville est juste avant ("PARIS 75019").
        # On prend le dernier mot alphabétique ; les villes multi-mots dans
        # ce format rare seront tronquées, le CP reste là pour lever le doute.
        avant = re.findall(r"[a-zA-ZÀ-ÿ'\-]{2,}", adresse[: m.start()])
        ville_brute = avant[-1] if avant else ""

    # nettoyage version affichage : Cedex et arrondissement retirés
    ville = re.sub(r"\s+cedex.*$", "", ville_brute, flags=re.IGNORECASE).strip()
    ville = re.sub(r"\s+\d+(e|er|ème|eme)?$", "", ville, flags=re.IGNORECASE).strip()
    return cp, ville.title(), normalize_city(ville_brute)


# libellés français pour les modes d'occupation de l'API
_OCCUPATION = {
    "alone": "Individuel",
    "couple": "En couple",
    "colocation": "Colocation",
    "shared": "Colocation",
}


def _format_prix(occupation_modes):
    """occupationModes[].rent.min est en CENTIMES -> "268,78 €" (le moins cher)."""
    prix = [
        om["rent"]["min"]
        for om in occupation_modes or []
        if om.get("rent") and om["rent"].get("min") is not None
    ]
    if not prix:
        return None
    euros = min(prix) / 100
    return f"{euros:.2f}".replace(".", ",") + " €"


def _format_surface(area):
    """{"min": 9.0, "max": 12.0} -> "9 m²" ou "de 9 à 12 m²"."""
    if not area or area.get("min") is None:
        return None
    lo, hi = area["min"], area.get("max") or area["min"]
    fmt = lambda x: f"{x:g}"
    return f"{fmt(lo)} m²" if lo == hi else f"de {fmt(lo)} à {fmt(hi)} m²"


def parse_api_item(it):
    """
    Un item JSON de l'API -> dict :
    {id, titre, adresse, cp, ville, ville_norm, prix, surface, details,
     tres_demande, dernieres_places, lien}
    """
    residence = it.get("residence") or {}
    adresse = residence.get("address") or ""
    cp, ville, ville_norm = extract_city(adresse)

    # "Amboise — T2" : nom de la résidence + type de logement
    nom_res = (residence.get("label") or "").strip()
    type_log = (it.get("label") or "").strip()
    titre = f"{nom_res} — {type_log}" if nom_res and type_log else (nom_res or type_log or "Logement")

    modes = sorted({
        _OCCUPATION.get(om.get("type"), om.get("type"))
        for om in it.get("occupationModes") or []
        if om.get("type")
    })

    # photo : la chambre en priorité ; si elle n'en a pas, celle de la
    # résidence en secours (~16 % des cas). Aucune des deux -> notif texte.
    medias = (it.get("medias") or []) + (residence.get("medias") or [])
    photo = config.MEDIA_BASE_URL + medias[0]["src"] if medias else None

    return {
        "id": str(it["id"]),
        "titre": titre,
        "adresse": adresse,
        "cp": cp,
        "ville": ville or "?",
        "ville_norm": ville_norm,
        "prix": _format_prix(it.get("occupationModes")),
        "surface": _format_surface(it.get("area")),
        "details": modes,
        "tres_demande": bool(it.get("highDemand")),
        "dernieres_places": bool(it.get("lowStock")),
        # défaut à False (pas True) si le champ manque un jour : en cas de
        # doute, mieux vaut rater une notif que d'en inventer une fausse
        "disponible": bool(it.get("available", False)),
        "photo": photo,
        "lat": (residence.get("location") or {}).get("lat"),
        "lon": (residence.get("location") or {}).get("lon"),
        "lien": f"https://trouverunlogement.lescrous.fr/tools/{config.TOOL_ID}/accommodations/{it['id']}",
    }
