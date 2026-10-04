# Déploiement du CROUS bot sur un VPS (Debian 12)

Objectif : le bot tourne 24/7 dans un datacenter et redémarre tout seul
(crash, reboot). Durée totale : ~20-30 min. Les commandes se tapent
telles quelles.

---

## 0. Ce qu'il te faut

- Un VPS. Recommandé : **Hetzner** (hetzner.com/cloud) — serveur **CX22
  ou CPX11** (~4-5 €/mois), image **Debian 12**, datacenter Falkenstein
  ou Nuremberg. (OVH/Scaleway marchent pareil ; le bot est minuscule,
  le plus petit plan suffit largement.)
- À la création : choisis un mot de passe root ou (mieux) ta clé SSH.
- **Note l'adresse IP** du serveur → remplace `IP_DU_VPS` partout ci-dessous.

## 1. Première connexion et préparation du serveur

Depuis PowerShell sur ton PC :

```
ssh root@IP_DU_VPS
```

Puis, sur le serveur :

```bash
apt update && apt -y upgrade
apt -y install python3-venv
timedatectl set-timezone Europe/Paris     # sinon rapport quotidien à minuit UTC
adduser --disabled-password --gecos "" crous
```

## 2. Copier le projet depuis ton PC

⚠️ **Ordre important pour ne perdre aucun inscrit** : si le bot tourne
encore sur ton PC, ARRÊTE-LE d'abord (Ctrl+C), PUIS copie. Deux bots
avec le même token = conflit Telegram (`Conflict: terminated by other
getUpdates request`) + doubles notifications.

Depuis PowerShell (pas dans le ssh) :

```
scp -r "C:\Users\miisaw\Desktop\telegram-bot-Crous\crous-bot" root@IP_DU_VPS:/home/crous/
```

De retour dans le ssh :

```bash
chown -R crous:crous /home/crous/crous-bot
# les fichiers écrits depuis Windows peuvent avoir des fins de ligne CRLF :
sed -i 's/\r$//' /home/crous/crous-bot/deploy/*.service
```

Vérifie que sont bien présents : `data.db` (tes inscrits actuels !),
`Cookies_Hard.JSON` (vue boursier) et `config.py` (token).

## 3. Environnement Python + test à la main

```bash
su - crous
cd crous-bot
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python main.py
```

Tu dois voir les logs de scan défiler ([public] 1423 disponibles...).
Envoie /status au bot depuis Telegram pour confirmer, puis **Ctrl+C**
et `exit` (retour root).

## 4. Service systemd (démarrage auto + relance auto)

```bash
cp /home/crous/crous-bot/deploy/crous-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now crous-bot
systemctl status crous-bot          # doit être "active (running)"
```

Suivre les logs en direct : `journalctl -u crous-bot -f`
Redémarrer après une modif : `systemctl restart crous-bot`

---

## Backup manuel (en attendant un backup automatique)

Tout l'état du bot (abonnés, accès payants, logements connus) tient
dans **`data.db`**. Pour en tirer une copie sur ton PC quand tu veux,
depuis PowerShell :

```
scp root@IP_DU_VPS:/home/crous/crous-bot/data.db "C:\Users\miisaw\Desktop\"
```

Restauration sur n'importe quelle machine : poser ce `data.db` à côté
de `main.py` et relancer — le bot reprend exactement où il en était.

## Pense-bête exploitation

- **7 juillet (rush)** : pour resserrer le scan, éditer `config.py`
  (`SCAN_INTERVAL`) puis `systemctl restart crous-bot`.
- **Cookies boursier expirés** (alerte 🍪 du bot) : remplacer
  `Cookies_Hard.JSON` sur le serveur (scp), puis restart.
- **Mise à jour du code** : scp des .py modifiés, puis restart.
- Santé générale : le rapport quotidien Telegram à minuit + l'alerte
  cycle long font office de monitoring de base.
