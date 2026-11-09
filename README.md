# C411 Library Checker · Vérificateur de bibliothèque C411

A **French / English**, read-only movie-library inventory for comparing local movie titles against [C411](https://c411.org/) via the Torznab API. Designed for Docker on UGREEN UGOS Pro (works on other Docker hosts).

Application **française / anglaise**, en lecture seule, permettant de comparer les titres de films d'un dossier local avec les résultats de recherche C411 via Torznab.

> **Important / Important :** "Not found" / « Introuvable » means **no result found by the current searches**, not definitive proof that C411 has no release. "Found" indicates a likely **title-level match**, not file or torrent-hash equivalence.

## Features / Fonctionnalités

- French / English language switch, stored in the browser / Bascule FR / EN.
- Recursively scans a specified folder within the `/media` read-only mount / Analyse récursive d'un dossier sous `/media`.
- Extracts movie titles and years from releases, Plex-style file names and local Kodi `.nfo` files / Lit les noms et fichiers `.nfo` locaux.
- Searches C411 through its Torznab API (`t=movie`, category `2000`).
- Displays **Found**, **Not found**, **Needs review**, **Error**, **Pending** / **Trouvé**, **Introuvable**, **À vérifier**, **Erreur**, **En attente**.
- Persistent SQLite progress, 30-day search cache and CSV export / Reprise, cache et export CSV.
- Files are never renamed, copied, deleted or downloaded / Aucune modification des films.
- Skips conventional TV episode names (`S01E01`, `1x01`) in this **movie-only** version / Ignore les épisodes TV classiques.
- Basic Auth login / Authentification HTTP Basic. API key stays on server / La clé API reste sur le serveur.

## Installation on UGREEN / Installation sur UGREEN

1. Transfer this project folder onto your NAS SSD app volume, for example `/volume2/docker/c411-library-checker` (actual UGOS path may differ).
2. Run `cp .env.example .env` and edit `.env`: set your **own** C411 API key and a unique strong `APP_PASSWORD`.
   - Get the key from your C411 account **Integrations** settings: `https://c411.org/user/integrations`.
   - Do not publish `.env`.
3. Edit the left-hand side of the movie mount in `compose.yaml`:

   ```yaml
   - /actual/path/on/nas/Films:/media:ro
   ```

   Use your real UGREEN absolute movie-directory path. Keep `:ro`.
4. Create the writable `config` folder on the SSD application volume and grant the container UID **10001** permission to write to it:

   ```sh
   mkdir -p config
   sudo chown 10001:10001 config
   ```

   On UGOS, set appropriate permissions through the system UI if preferred.
5. Start from the project directory:

   ```sh
   docker compose up -d --build
   ```

6. Visit `http://NAS-IP:8787` from your **trusted LAN** and sign in using `APP_USERNAME` / `APP_PASSWORD`.
7. Select `.` to scan all files in the mounted `/media` directory, or a subfolder such as `Films/Action`; choose **Start scan** / « Lancer l'analyse ».

The app is intentionally **not designed for exposure to the public Internet**. It uses HTTP Basic auth; use an HTTPS reverse proxy with additional authentication or a VPN for remote use. Do not forward port 8787 from your router.

## Performance / Performances

- The tool sends **at most one C411 search every 4.5 seconds**. A collection with thousands of unique titles may take several hours.
- It caches searches for 30 days and preserves results of unchanged files to avoid repeated requests.
- If a scan is interrupted, simply start it again; unchanged results remain saved.
- For renamed films, it first checks an accompanying `.nfo` when available; unmatched or ambiguous titles are flagged for manual review.
- Movie filename heuristics are intentionally conservative. French vs original title, multipart films, anime and unconventional naming may need manual review.

## Security / Sécurité

- The mounted media directory is `:ro` and all API folder selections are restricted to that location.
- No torrent downloads, no qBittorrent control, no media mutation.
- Never commit `.env`; it contains your API key and password.
- Docker container runs as a non-root user.
- Back up `/config/checker.sqlite3` with your other application data.

## API / Endpoints

- `GET /api/status` — counts and scan state
- `GET /api/folders` — subdirectories available for scanning
- `POST /api/scan` — `{ "folder": ".", "refresh": false }`
- `POST /api/stop` — stop a running scan
- `GET /api/movies?status=missing&limit=50&offset=0&q=` — results
- `GET /api/export.csv?status=missing` — CSV export

All except `/health` require HTTP Basic Auth.

## Development / Développement

The backend uses **Python 3.12 standard library only**, with a static HTML/CSS/JS interface; no external Python dependencies or CDNs. To run tests:

```sh
python -m unittest discover -s tests -v
```

## Disclaimer

This is a personal catalog comparison utility. Only access the tracker using your authorized API credentials and respect the service's limits and rules, as well as applicable copyright rules.
