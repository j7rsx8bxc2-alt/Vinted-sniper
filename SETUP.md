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

## 3a. Persistenten Speicher einrichten (Railway Volume)

Ohne diesen Schritt gehen deine Snipe-Bot-Suchen (`!add`), deine Buchhaltung und dein
Trend-Radar-Verlauf bei **jedem Redeploy** verloren, weil Railway den Dateisystem-Inhalt
eines Containers sonst nicht dauerhaft speichert. Genau das war die Ursache, als der
Snipe-Bot plötzlich "Keine Suchen aktiv" meldete.

**Volume anlegen:**
1. Railway-Projekt öffnen → deinen Bot-Service anklicken
2. Tab **Settings** → runter scrollen zu **Volumes** → **+ New Volume**
3. Als **Mount Path** eingeben: `/data`
4. Speichern — Railway startet den Service neu

**Variables ergänzen** (Tab **Variables**, zusätzlich zu den obigen):

| Variable | Wert |
|---|---|
| `MONITOR_URLS_FILE` | `/data/monitor_urls.json` |
| `BUCHHALTUNG_DB` | `/data/buchhaltung.db` |
| `TRENDS_DB` | `/data/trends.db` |
| `PROXIES_FILE` | `/data/proxies.txt` (nur falls genutzt) |

Danach nochmal `!add ...` für alle deine Kanäle ausführen (einmalig) — ab jetzt bleibt
alles auch nach jedem Redeploy erhalten, weil es auf dem Volume statt im flüchtigen
Container-Speicher landet.

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
├── vinted_bot.py           ← Haupt-Bot (Sniper) + lädt die Erweiterungen unten
├── cogs/
│   ├── buchhaltung.py      ← Einkauf/Verkauf/Marge-Tracking (SQLite) + Verkauft-Check
│   ├── listing.py          ← Interaktives Inserat-Erstellen
│   ├── vinted_client.py    ← Experimenteller Vinted-API-Client für Auto-Post
│   ├── ai_vision.py        ← KI-Bildanalyse für !inserat (Titel/Marke/Beschreibung/Hashtags)
│   ├── openrouter_client.py← Gemeinsamer KI-Client (Text + Vision)
│   ├── coach.py            ← !coach – Reselling-Tipps von der KI
│   └── price_check.py      ← !preischeck – Preisspanne ähnlicher Vinted-Angebote
├── requirements.txt        ← Python-Abhängigkeiten
├── Procfile                ← Railway Start-Befehl
├── runtime.txt             ← Python-Version für Railway
├── .gitignore              ← schützt .env, monitor_urls.json, *.db
└── .env.example            ← Vorlage für lokale Entwicklung
```

---

## 6. Buchhaltungs-Bot

Trackt jeden Artikel einzeln (Einkaufspreis, Verkaufspreis, Marge) in einer
lokalen SQLite-Datei.

| Command | Beschreibung |
|---|---|
| `!kauf <name> <preis> [notiz]` | Einkauf erfassen |
| `!verkauf <id> <preis>` | Als verkauft markieren, Marge wird berechnet |
| `!lager` | Alle offenen (unverkauften) Artikel |
| `!verkauft [tage]` | Verkäufe, optional nur letzte X Tage |
| `!artikel <id>` | Details zu einem Artikel |
| `!loeschen <id>` | Artikel löschen |
| `!bilanz [tage]` | Gesamtübersicht: Umsatz, Gewinn, Lagerwert |
| `!export` | Alles als CSV exportieren |
| `!vinted-link <id> <url/id>` | Artikel mit einem Vinted-Inserat verknüpfen |
| `!verkauft-check` | Manuell prüfen ob verknüpfte Artikel noch aktiv sind |

### Verkauft-Erkennung

Nachdem du ein Inserat manuell auf Vinted eingestellt hast, verknüpfe es mit
`!vinted-link <buchhaltungs-id> <vinted-link-oder-id>`. Der Bot prüft danach
automatisch alle `VERKAUFT_CHECK_MINUTES` Minuten (Standard 60), ob der
Artikel auf Vinted noch zu finden ist, und postet einen Alarm im
`VERKAUFT_CHECK_CHANNEL`, wenn er vermutlich verkauft/entfernt wurde. Trägt
den Verkauf **nicht automatisch** ein (der tatsächliche Verkaufspreis ist
über die Anfrage nicht zuverlässig auslesbar) — du bestätigst danach kurz mit
`!verkauf <id> <preis>`. Reine Lese-Anfrage (kein Auto-Post), funktioniert
also genau wie der Snipe-Bot zuverlässig, ganz ohne das Bot-Schutz-Problem
von oben.

**Wichtig bei Railway:** SQLite-Dateien gehen bei jedem Redeploy verloren,
wenn sie nicht auf einem [Volume](https://docs.railway.app/reference/volumes)
liegen. Richte ein Volume ein und zeig `BUCHHALTUNG_DB` in den Railway-Variablen
darauf (z.B. `/data/buchhaltung.db`), sonst ist die Buchhaltung nach jedem
Deploy leer.

---

## 7. Listing-Bot

`!inserat` startet einen geführten Frage-Flow: erst schickst du 1-4 Fotos,
dann (falls `OPENROUTER_API_KEY` gesetzt ist) schlägt eine KI automatisch
Titel, Marke und eine Vinted-optimierte Beschreibung mit Hashtags vor — du
bestätigst mit `ja` oder korrigierst. Danach fragt der Bot nur noch Größe,
Zustand, Verkaufspreis und Einkaufspreis ab. Am Ende steht ein fertiges
Listing im Channel `LISTING_CHANNEL_NAME` (Standard: `#verkauf`) und der
Artikel ist mit dem echten Einkaufspreis in der Buchhaltung angelegt.

### Kostenlosen KI-Key einrichten (für automatische Titel/Beschreibung)

1. Gehe zu [openrouter.ai/keys](https://openrouter.ai/keys)
2. Registrieren per E-Mail, Google oder GitHub (kein Kreditkarte nötig)
3. **Create Key** klicken → Key kopieren (wird nur einmal angezeigt!)
4. In `.env` bei `OPENROUTER_API_KEY=` einfügen
5. Bot neu starten

`OPENROUTER_MODEL=openrouter/free` lässt OpenRouter automatisch ein
passendes Gratis-Modell mit Bildverständnis wählen. Ohne den Key
funktioniert `!inserat` trotzdem — du tippst Titel/Marke/Beschreibung dann
einfach manuell ein.

**Automatisches Vinted-Inserat (deaktiviert):** Der Code dafür existiert noch
(`cogs/vinted_client.py`, aktiviert über `VINTED_SESSION_COOKIE`), wird aber
aktuell von Vinteds Bot-Schutz (Datadome) mit HTTP 403 "access_denied"
blockiert — das lässt sich nicht einfach durch API-Fixes lösen, sondern
bräuchte einen echten Browser-Automatisierungs-Ansatz (deutlich aufwendiger,
höheres Account-Risiko). Lass `VINTED_SESSION_COOKIE` leer, dann postet
`!inserat` nur in Discord und du stellst manuell auf Vinted ein — das
funktioniert zuverlässig und ist der empfohlene Weg.

---

## 8. Reselling-Coach

`!coach <Frage>` beantwortet Fragen rund ums Vintage-Reselling (Fotografie,
Preisstrategie, Verhandeln, Einkaufsstrategie, Marktkenntnis). Merkt sich die
letzten paar Nachrichten pro Person als Kontext — `!coach-reset` setzt das
zurück. Braucht denselben `OPENROUTER_API_KEY` wie `!inserat`.

## 9. Preis-Check

`!preischeck <Suchbegriff>` (z.B. `!preischeck ralph lauren strickpullover`)
durchsucht aktuelle Vinted-Angebote zum Suchbegriff und zeigt Min/Median/Max/
Durchschnittspreis plus ein paar Beispiel-Links. Das ist eine reine
Lese-Anfrage (wie der Snipe-Bot) — kein Cookie nötig, kein Bot-Schutz-Problem.
Basiert auf aktuellen Angebotspreisen, nicht auf bestätigten Verkaufspreisen
(Vinted stellt Sold-Daten nicht öffentlich bereit) — als Richtwert für die
eigene Preisfindung trotzdem hilfreich.

## 10. Virtual Try-On (experimentell – Stil-Vorschau, kein exaktes Abbild)

`!tryon` zieht ein fotografiertes Kleidungsstück per KI auf ein Model-Foto.
Bewusst als **ungefähre Stil-Vorschau** kommuniziert, nicht als exaktes
Produktfoto — Details wie Logos kann die KI abweichend oder dazuerfunden
darstellen (siehe Hinweis weiter unten). Für logo-relevante Artikel
zusätzlich das echte Produktfoto posten.
Nutzt eine kostenlose, community-gehostete Hugging-Face-Space (aktuell
`yisol/IDM-VTON`, die originale und mit Abstand beliebteste IDM-VTON-Space —
über `TRYON_SPACE` in `.env` austauschbar) — kein Key, kein Kreditkarte
nötig, dafür langsamer (30-90s) und weniger stabil als ein bezahlter Dienst.

Falls mal `RUNTIME_ERROR` oder eine andere Fehlermeldung von der Space
kommt: das ist ein Problem beim kostenlosen Community-Betreiber, nicht am
Bot-Code. Meist hilft warten und nochmal probieren; hält es länger an, in
`TRYON_SPACE` eine andere IDM-VTON-artige Space eintragen (auf huggingface.co
nach "IDM-VTON" oder "virtual try on" suchen, muss `api_name='tryon'` haben).

**Model-Fotos:** liegen im Ordner `assets/models/` — leg dort ein paar
KI-generierte, synthetische Fotos ab (z.B. von
[Bing Image Creator](https://www.bing.com/images/create) oder
[thispersondoesnotexist.com](https://thispersondoesnotexist.com)), dann
wählt `!tryon` bei jedem Aufruf automatisch zufällig eins davon.

⚠️ Bewusst **keine** echten Personenfotos von Google, Pinterest, Vinted o.ä.
reinlegen — das verletzt Urheber- und Persönlichkeitsrechte der abgebildeten
Menschen und ist ein echtes rechtliches Risiko für dein Business, gerade bei
vielen Nutzer:innen.

| Command | Beschreibung |
|---|---|
| `!tryon` | Nutzt automatisch ein zufälliges Foto aus `assets/models/` |
| `!tryon eigenes` | Erzwingt die manuelle Abfrage (eigenes Model-Foto schicken) |
| `!tryon-modelle` | Zeigt wie viele Model-Fotos aktuell hinterlegt sind |

**Zu Logos/Prints:** der Bot lässt die KI das Kleidungsstück vorher kurz
beschreiben (Farbe, Schnitt, Logo-Position) und gibt das an IDM-VTON weiter —
das hilft etwas, aber IDM-VTON ist ein generatives Modell, kein 1:1-Ausschneiden
und -Einfügen. Kleine Logos, Schriftzüge und Muster werden deshalb grundsätzlich
nie exakt originalgetreu übernommen, auch mit guter Beschreibung nicht — das
ist eine bekannte Grenze aller aktuellen kostenlosen Try-On-Modelle, keine
Fehlfunktion des Bots. Für Artikel bei denen das Logo/der Print das Wichtigste
am Foto ist, bleibt ein normales Flat-Lay-Foto (ohne Try-On) oft die bessere Wahl.

Voraussetzung: `pip3 install -r requirements.txt` (neue Abhängigkeit
`gradio_client`). Dieses Feature wurde nicht live gegen den echten Dienst
getestet — beim ersten Ausprobieren wahrscheinlich gemeinsames Debugging
nötig.

## 11. Kanal-Hilfe

`!kanal-hilfe` postet und pinnt eine kurze Erklärung, was man im aktuellen
Kanal tun kann (erkennt Kanalnamen wie verkauf/buchhaltung/coach/preis/sold
automatisch). Einmal pro Kanal manuell ausführen. Braucht die Bot-Berechtigung
**"Nachrichten verwalten"** im jeweiligen Kanal zum Anpinnen.

## 12. Willkommensnachricht bei Server-Join

Sobald jemand deinem Server beitritt, postet der Bot automatisch eine kurze,
freundliche Übersicht im Kanal `WELCOME_CHANNEL_NAME` (Standard: sucht nach
einem Kanal dessen Name "chat-all" enthält, findet also auch z.B.
"🇩🇪-chat-all").

**Wichtig – zusätzlicher Schritt im Discord Developer Portal:**
Dieses Feature braucht den privilegierten "Server Members Intent", der
standardmäßig aus ist. Ohne diesen Schritt startet der Bot gar nicht mehr
(Fehlermeldung `PrivilegedIntentsRequired`):

1. Gehe zu [discord.com/developers/applications](https://discord.com/developers/applications)
2. Deine Bot-Anwendung auswählen → Tab **Bot**
3. Runterscrollen zu **Privileged Gateway Intents**
4. **SERVER MEMBERS INTENT** aktivieren, speichern
5. Bot neu starten

Zusätzlich: bekommt ein Mitglied die VIP-Rolle erst **nachträglich** (z.B. weil
sie erst nach der Whop-Zahlung automatisch vergeben wird, nicht direkt beim
Server-Beitritt), postet der Bot die goldene VIP-Willkommensnachricht mit GIF
trotzdem — dann eben im Moment der Rollenvergabe statt beim Beitritt.

## 13. Zugriffskontrolle (wer darf welche Befehle nutzen)

Zwei Berechtigungsstufen, geregelt in `cogs/access.py`:

- **Premium-Befehle** (`!kauf`, `!verkauf`, `!lager`, `!bilanz`, `!export`,
  `!inserat`, `!coach`, `!preischeck`, `!tryon`, usw.): nur Mitglieder mit der
  VIP-Rolle oder Server-Admins.
- **Snipe-Bot-Befehle** (`!add`, `!remove`, `!list`, `!proxy`): nur
  Server-Admins — diese verändern die geteilte Such-Konfiguration für den
  ganzen Server, daher niemand sonst.

Wer keine Berechtigung hat und den Befehl trotzdem eintippt, bekommt **keine
Fehlermeldung** — der Befehl wird stillschweigend ignoriert.

Die VIP-Rolle wird über den Rollennamen erkannt (Groß-/Kleinschreibung egal).
Standard ist `VIP`, änderbar über die Env-Variable `VIP_ROLE_NAME` in der
`.env`, falls deine Rolle anders heißt. Mit `!meine-rollen` kannst du prüfen,
welche Rollen ein Mitglied hat und ob die VIP-Erkennung anschlägt.

## 14. Foto-Check

`!fotocheck` — schick 1-4 Fotos, die KI bewertet sie mit Score (1-10) plus
konkreten Tipps (Licht, Hintergrund, Bildausschnitt, fehlende Perspektiven
wie Rückseite/Etikett). Läuft außerdem automatisch als kurzer Zwischenschritt
in `!inserat`, direkt nachdem die Fotos hochgeladen wurden — kein
zusätzlicher Schritt nötig. Braucht denselben `OPENROUTER_API_KEY` wie
`!inserat`/`!coach`.

## 15. Trend-Radar

Läuft komplett automatisch, KEIN manuelles Eintragen von Begriffen nötig:
der Bot trackt automatisch genau die Marken/Kategorien, die im Snipe-Bot
aktive Suchen haben (also dieselben Kanäle wie bei `!add`/`!list`). Sobald
du per `!add` einen neuen Kanal mit Suche anlegst, taucht er automatisch im
Trend-Radar mit auf — entfernst du die letzte Suche eines Kanals (`!remove`),
fällt er dort auch automatisch wieder raus.

Zwei Signale pro Post:
- **Angebots-Bewegung (Vinted)**: wie viele aktive Inserate es pro Marke/Kategorie
  gerade gibt und ob das steigt oder fällt — ein Proxy für Konkurrenz/Interesse,
  KEINE echte Verkaufszahl (die veröffentlicht Vinted nicht).
- **"Bei dir am besten weg"**: gleicht automatisch mit deiner Buchhaltung ab —
  wie oft Artikel mit passendem Namen (z.B. Marke) bei DIR tatsächlich verkauft
  wurden und wie schnell im Schnitt. Das sind echte eigene Verkäufe, kein Proxy.

Commands (alle optional, nur für Ad-hoc-Checks oder Extras — die Snipe-Bot-Kanäle
laufen von selbst):

- `!trends <Suchbegriff>` — Ad-hoc-Check für einen beliebigen Begriff, unabhängig vom Snipe-Bot
- `!trends-add <Suchbegriff>` — zusätzlichen Begriff mittracken, über die Snipe-Bot-Kanäle hinaus
- `!trends-entfernen <Suchbegriff>` — einen so hinzugefügten Zusatz-Begriff wieder entfernen
- `!trends-liste` — zeigt, was aktuell automatisch + manuell getrackt wird
- `!trends-tagesupdate` — Test: zeigt sofort, wie das tägliche Update aussieht
- `!trends-wochenrecap` — Test: zeigt sofort, wie der Sonntags-Recap aussieht
- `!trends-hilfe` — Übersicht aller Trend-Radar-Commands

Automatische Posts in `TRENDS_CHANNEL_NAME` (Standard: "trend-radar" — Kanal
anlegen und dem Bot dort Sende-Rechte geben, sonst kommt nichts an):

- **Täglich** um `TREND_DAILY_HOUR` Uhr (Standard: 8, morgens) — Vergleich zum letzten Check
- **Jeden Sonntag** um `TREND_WEEKLY_HOUR` Uhr (Standard: 18, abends) — Wochenrecap, Vergleich
  zum Stand vor 7 Tagen

Beide Uhrzeiten sind Europe/Berlin (Sommer-/Winterzeit wird automatisch
berücksichtigt) und über die `.env` änderbar. Mit `!trends-tagesupdate` bzw.
`!trends-wochenrecap` kannst du dir jederzeit sofort ansehen, wie so ein Post
aussieht, ohne auf die Uhrzeit zu warten — vorausgesetzt der Snipe-Bot hat
schon mindestens einen Kanal mit aktiver Suche.

## 16. Eigene VIP-Kanäle für Foto-Check, Trend-Radar & Virtual Try-On

`!fotocheck`, `!trends` und `!tryon` funktionieren technisch in jedem Kanal
(der VIP-Check läuft ja sowieso pro Befehl). Für mehr Exklusivität — wie bei
euren anderen Premium-Kanälen — könnt ihr trotzdem eigene Kanäle anlegen,
die NICHT-VIPs gar nicht erst sehen:

1. Rechtsklick auf eine Kategorie → **Kanal erstellen** → Name `foto-check`
   (die anderen beiden genauso mit den Namen `trend-radar` und `try-on`)
2. Rechtsklick auf den neuen Kanal → **Kanal bearbeiten** → Tab **Berechtigungen**
3. Bei **@everyone**: **"Kanal ansehen"** auf ❌ (verweigert) stellen — damit ist
   der Kanal für alle unsichtbar
4. Über das **+** deine VIP-Rolle hinzufügen → dort **"Kanal ansehen"**,
   **"Nachrichten senden"** und **"Nachrichtenverlauf anzeigen"** auf ✅ stellen
5. Über das **+** auch den Bot (seine Rolle, meist der Name deiner Bot-App)
   hinzufügen → dieselben drei Häkchen wie bei VIP, sonst kann der Bot dort
   nicht antworten (bei `trend-radar` wichtig, weil das automatische Update
   dort reinkommt)
6. Für `trend-radar` wichtig: der Kanalname muss exakt zu `TRENDS_CHANNEL_NAME`
   in der `.env` passen (aktuell auf `trend-radar` gesetzt) — sonst findet der
   Bot den Kanal fürs automatische Update nicht.

Für `foto-check` und `try-on` braucht ihr keine Env-Variable einzustellen —
der Kanalname ist rein organisatorisch, die Befehle selbst reagieren überall
wo ein VIP sie eintippt. In `try-on` einmal `!kanal-hilfe` eintippen, dann
steht die Erklärung oben angepinnt.
