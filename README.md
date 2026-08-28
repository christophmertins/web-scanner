# hp-scanner-web

Docker-basierte Webanwendung zum Scannen mit einem HP Officejet 4500
(G510) über das Netzwerk. Container läuft auf dem Raspberry Pi
(`root@10.0.0.225`), spricht den Scanner per SANE/HPLIP an
(`hpaio://192.168.1.107`).

## Funktionen

- Einzelscan als **JPEG, PNG** oder **PDF**
- **ADF-Scan** (Dokumenteneinzug): alle Seiten in einem Durchzug → PDF/ZIP
- Mehrseitiges **PDF** (jeweils „Nächste Seite“ scannen, dann zusammenführen)
- Wählbar: Modus (Farbe/Graustufen/Lineart), Auflösung (100–600 dpi),
  Scanbereich (A4 oder Gerät-maximal)
- Vorschau, Download, Galerie der bisherigen Scans
- „An Paperless senden“ direkt nach dem Scan **und nachträglich aus Galerie/Verlauf**
- Optionaler Basic-Auth-Schutz

## Deployment

```bash
scp -r Dockerfile docker-compose.yml entrypoint.sh app .env.example root@10.0.0.225:/opt/scanner-web/
ssh root@10.0.0.225 "cd /opt/scanner-web && cp .env.example .env && vi .env"
ssh root@10.0.0.225 "cd /opt/scanner-web && docker compose up -d --build"
```

Weboberfläche: `http://10.0.0.225:8000` (im LAN)

Scans landen in `/opt/scanner-web/scans/` (Volume `./scans`).

## Konfiguration

Geheimnisse (`PAPERLESS_URL`, `PAPERLESS_TOKEN`, `AUTH_USER`, `AUTH_PASS`)
werden über eine **`.env`**-Datei gesetzt (siehe `.env.example`), die nicht
versioniert wird. Gerätespezifische Werte stehen in `docker-compose.yml`.

| Variable | Bedeutung |
|----------|-----------|
| `SCANNER_IP` | IP des Scanners (Default `192.168.1.107`) |
| `SCANNER_DEVICE` | Explizite hpaio-Device-URI; leer lassen für Auto-Erkennung |
| `AUTH_USER` / `AUTH_PASS` | Optional Basic Auth für die Webanwendung |
| `SCAN_TIMEOUT` | Timeout je Scan in Sekunden (Default 120) |
| `PAPERLESS_URL` | Basis-URL der Paperless-ngx Instanz (ohne Trailing-Slash) |
| `PAPERLESS_TOKEN` | Paperless API-Token (Profil → Authentifizierungstoken) |

Paperless: Nach einem Scan erscheint der Button „An Paperless senden“
(Upload über die Paperless-API; je moderat, danach übernimmt Paperless OCR/Ablage).
Konfiguriert ist `PAPERLESS_URL=https://paperless.haenf.duckdns.org` (valid
Let's-Encrypt-Wildcard-Cert über den lokalen nginx-proxy).

Datum der Device-URI enthält ein `&` -> in YAML als `&amp;` escapen.

## Technik

- Basisimage: `debian:bookworm-slim` (ARM64-kompatibel)
- SANE Backends `hpaio` (HPLIP) + `sane-airscan`
- `network_mode: host` (nötig für Scanner-Discovery im Container)
- D-Bus Systembus + Avahi werden im Entrypoint gestartet (hpaio braucht beide)
- Flask (`app/app.py`), mehrseitige PDFs werden mit Pillow zusammengefügt

## CI / Docker-Image

GitHub Actions (`.github/workflows/docker-build.yml`) baut das Image bei jedem
Push/PR als Multi-Arch-Build (`linux/amd64`, `linux/arm64`). Auf `main` und bei
Tags wird es unter `ghcr.io/<repo>:latest` (bzw. `:sha-…`, `:v…`) gepusht;
PRs bauen nur. Fetch des Image auf dem Pi:

```bash
docker pull ghcr.io/christophmertins/web-scanner:latest
```

Lokaler Build bleibt `docker compose up -d --build`.