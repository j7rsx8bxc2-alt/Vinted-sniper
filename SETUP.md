# Vinted Sniper – Setup Guide

## 1. GitHub Repo anlegen

```bash
git init
git add .
git commit -m "initial commit"
# Neues Repo auf github.com erstellen, dann:
git remote add origin https://github.com/DEIN_NAME/vinted-sniper.git
git push -u origin main
```

---

## 2. Railway Projekt erstellen

1. Gehe zu [railway.app](https://railway.app) → **New Project**
2. Wähle **Deploy from GitHub repo**
3. Dein `vinted-sniper` Repo auswählen
4. Railway erkennt den `Procfile` automatisch (`worker: python vinted_bot.py`)

---

## 3. Environment Variables in Railway setzen

Gehe in Railway zu deinem Service → **Variables** → folgende eintragen:

| Variable | Wert |
|---|---|
| `DISCORD_TOKEN` | Dein Discord Bot Token |
| `WEBSHARE_USER` | Dein Webshare Benutzername |
| `WEBSHARE_PASS` | Dein Webshare Passwort |

> ⚠️ Die `.env`-Datei **nicht** committen – sie ist in `.gitignore` ausgeschlossen.

---

## 4. Webshare Proxy einrichten

1. Gehe zu [proxy.webshare.io](https://proxy.webshare.io)
2. Links im Menü: **Rotating Proxy** → **Endpoint Generator**
3. Notiere dir:
   - **Proxy Username** → `WEBSHARE_USER`
   - **Proxy Password** → `WEBSHARE_PASS`
   - Host ist immer `p.webshare.io:80` (bereits im Code eingetragen)

---

## 5. Deploy & testen

Nach dem Setzen der Variables startet Railway automatisch neu.

Im Discord kannst du testen:
```
!proxy     → zeigt ob Webshare aktiv ist
!help      → alle Befehle
!add nike https://www.vinted.de/catalog?...
```

---

## Dateistruktur

```
vinted-sniper/
├── vinted_bot.py       ← Haupt-Bot
├── requirements.txt    ← Python-Abhängigkeiten
├── Procfile            ← Railway Start-Befehl
├── runtime.txt         ← Python-Version für Railway
├── .gitignore          ← schützt .env und monitor_urls.json
└── .env.example        ← Vorlage für lokale Entwicklung
```
