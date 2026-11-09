#!/usr/bin/env python3
"""C411 Library Checker: local, read-only media inventory + Torznab lookup."""
from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/media")).resolve()
DATA_DIR = Path(os.environ.get("DATA_DIR", "/config")).resolve()
API_URL = os.environ.get("C411_API_URL", "https://c411.org/api/torznab").strip()
API_KEY = os.environ.get("C411_API_KEY", "").strip()
APP_USER = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8787"))
REQUEST_INTERVAL = max(4.2, float(os.environ.get("REQUEST_INTERVAL_SECONDS", "4.5")))
CACHE_DAYS = max(0, int(os.environ.get("CACHE_DAYS", "30")))
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".ts", ".m2ts", ".webm", ".iso"}
YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
EPISODE_RE = re.compile(r"(?i)\bS\d{1,2}[ ._-]?E\d{1,3}\b|\b\d{1,2}x\d{2}\b")
RELEASE_RE = re.compile(r"(?i)\b(480p|720p|1080p|2160p|4k|8k|bluray|blu-ray|bdrip|brrip|webrip|web-?dl|hdrip|dvdrip|remux|x264|x265|h264|h265|hevc|av1|aac|dts|atmos|hdr|dv|multi|vostfr|french|truefrench|proper|repack|extended|unrated|director.?s.cut)\b")
RATE_LOCK = threading.Lock()
LAST_REQUEST = [0.0]
WORK_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
WORKER = [None]
STATE = {"running": False, "phase": "idle", "folder": "", "processed": 0, "total": 0, "error": "", "started_at": "", "ended_at": ""}


def db():
    connection = sqlite3.connect(DATA_DIR / "checker.sqlite3", timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def initialize():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS movies (
            path TEXT PRIMARY KEY, title TEXT NOT NULL, original_title TEXT NOT NULL DEFAULT '',
            year INTEGER, size INTEGER NOT NULL, mtime REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', match_title TEXT NOT NULL DEFAULT '',
            score REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
            checked_at INTEGER NOT NULL DEFAULT 0, seen TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS searches (
            query TEXT PRIMARY KEY, payload TEXT NOT NULL, checked_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_movies_status ON movies(status);
        """)


def safe_directory(folder: str) -> Path:
    """Accept only paths beneath the mounted media folder; disallow symlink traversal."""
    if not isinstance(folder, str) or '\0' in folder or len(folder) > 3000:
        raise ValueError("Invalid directory / Dossier invalide")
    if folder.startswith("/"):
        candidate = Path(folder)
    else:
        candidate = MEDIA_ROOT / folder
    resolved = candidate.resolve()
    try:
        resolved.relative_to(MEDIA_ROOT)
    except ValueError as exc:
        raise ValueError("Folder must be inside /media / Le dossier doit être sous /media") from exc
    if not resolved.is_dir():
        raise ValueError("Folder not found / Dossier introuvable")
    return resolved


def clean_title(value: str) -> str:
    value = re.sub(r"[._]+", " ", value)
    value = re.sub(r"[\[\]{}()]", " ", value)
    value = RELEASE_RE.split(value)[0]
    return re.sub(r"\s+", " ", value).strip(" -_")


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"\b(the|le|la|les|un|une|a|an)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def metadata(video: Path) -> tuple[str, str, int | None]:
    """Prefer Kodi/Jellyfin-style NFO where present, otherwise parse release and year."""
    title, original, year = "", "", None
    nfos = [video.with_suffix(".nfo"), video.parent / "movie.nfo"]
    for path in nfos:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
            continue
        try:
            root = ET.parse(path).getroot()
            if root.tag not in ("movie", "episodedetails"):
                continue
            title = (root.findtext("title") or "").strip()
            original = (root.findtext("originaltitle") or "").strip()
            raw_year = (root.findtext("year") or "").strip()
            if raw_year and YEAR_RE.search(raw_year):
                year = int(YEAR_RE.search(raw_year).group(1))
            break
        except (OSError, ET.ParseError, ValueError):
            continue
    stem = video.stem
    parent = video.parent.name
    # Parent folder often contains the full film title when video basename is generic.
    source = parent if stem.lower() in ("movie", "film", "video", "main", "feature") else stem
    y = YEAR_RE.search(source) or YEAR_RE.search(parent)
    if year is None and y:
        year = int(y.group(1))
    if not title:
        part = YEAR_RE.split(source)[0] if YEAR_RE.search(source) else source
        title = clean_title(part) or clean_title(parent) or stem
    if not original:
        original = ""
    return title[:250], original[:250], year


def score_candidate(title: str, original: str, year: int | None, release: str) -> float:
    matched_title = 0.0
    release_title = clean_title(YEAR_RE.split(release)[0] if YEAR_RE.search(release) else release)
    for candidate in (title, original):
        if candidate:
            a, b = normalize(candidate), normalize(release_title)
            if a and b:
                matched_title = max(matched_title, SequenceMatcher(None, a, b).ratio())
    published = YEAR_RE.search(release)
    if published and year and int(published.group(1)) != year:
        return min(matched_title * 0.45, 0.44)
    if published and year and int(published.group(1)) == year:
        return matched_title
    # Without a year in both filename and result, never auto-confirm unless original name identical.
    return min(matched_title * 0.85, 0.84)


def get_cached(conn: sqlite3.Connection, query: str):
    row = conn.execute("SELECT payload, checked_at FROM searches WHERE query=?", (query,)).fetchone()
    if row and row["checked_at"] >= int(time.time()) - CACHE_DAYS * 86400:
        return json.loads(row["payload"])
    return None


def search_c411(conn: sqlite3.Connection, query: str) -> list[str]:
    cached = get_cached(conn, query)
    if cached is not None:
        return cached
    with RATE_LOCK:
        remaining = REQUEST_INTERVAL - (time.monotonic() - LAST_REQUEST[0])
        if remaining > 0 and STOP_EVENT.wait(remaining):
            raise InterruptedError("Stopped")
        LAST_REQUEST[0] = time.monotonic()
        params = urlencode({"apikey": API_KEY, "t": "movie", "cat": "2000", "q": query, "limit": 100})
        req = Request(API_URL + ("&" if "?" in API_URL else "?") + params,
                      headers={"User-Agent": "C411-Library-Checker/0.1", "Accept": "application/xml"})
        try:
            with urlopen(req, timeout=25) as resp:
                body = resp.read(2_000_001)
            if len(body) > 2_000_000:
                raise RuntimeError("Response too large / Réponse trop volumineuse")
        except HTTPError as exc:
            raise RuntimeError(f"C411 HTTP {exc.code} (check API key or rate limit)") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError("Network error contacting C411 / Erreur réseau C411") from exc
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise RuntimeError("Invalid XML response from C411 / XML invalide") from exc
    error = root.find("error") if root.tag != "error" else root
    if error is not None:
        raise RuntimeError("C411 API error: " + str(error.attrib.get("code", "unknown")))
    results = []
    for item in root.findall(".//channel/item"):
        name = (item.findtext("title") or "").strip()
        if name:
            results.append(name[:350])
    with conn:
        conn.execute("INSERT OR REPLACE INTO searches(query,payload,checked_at) VALUES(?,?,?)",
                     (query, json.dumps(results), int(time.time())))
    return results


def classify(conn, title, original, year):
    candidates = []
    queries = list(dict.fromkeys([x for x in (title, original) if x and len(x) > 1]))
    for query in queries:
        candidates.extend(search_c411(conn, query))
        if STOP_EVENT.is_set():
            raise InterruptedError("Stopped")
        matches = [(score_candidate(title, original, year, item), item) for item in candidates]
        if matches and max(matches)[0] >= 0.89:
            break
    if not candidates:
        return "missing", "", 0.0
    best = max(((score_candidate(title, original, year, x), x) for x in candidates), key=lambda x: x[0])
    if best[0] >= 0.89:
        return "found", best[1], round(best[0], 3)
    return "review", best[1], round(best[0], 3)


def run_scan(folder, refresh):
    STATE.update({"running": True, "phase": "inventory", "processed": 0, "total": 0,
                  "error": "", "folder": folder, "started_at": datetime.now(timezone.utc).isoformat(), "ended_at": ""})
    token = str(time.time_ns())
    try:
        directory = safe_directory(folder)
        if not API_KEY or not APP_PASSWORD:
            raise RuntimeError("Missing C411_API_KEY or APP_PASSWORD in .env")
        with db() as conn:
            if refresh:
                conn.execute("DELETE FROM searches")
                conn.commit()
            for current, dirs, files in os.walk(directory, followlinks=False):
                if STOP_EVENT.is_set():
                    raise InterruptedError("Stopped")
                dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink() and not d.startswith(".")]
                for filename in files:
                    video = Path(current) / filename
                    if video.suffix.lower() not in VIDEO_EXT or video.is_symlink() or filename.startswith(".") or filename.lower().startswith("sample"):
                        continue
                    if EPISODE_RE.search(filename):
                        continue  # movie-only version; TV episodes are intentionally excluded
                    try:
                        stat = video.stat()
                        rel = str(video.relative_to(MEDIA_ROOT))
                        title, original, year = metadata(video)
                        old = conn.execute("SELECT size,mtime,status FROM movies WHERE path=?", (rel,)).fetchone()
                        unchanged = old and old["size"] == stat.st_size and old["mtime"] == stat.st_mtime
                        status = old["status"] if unchanged and not refresh else "pending"
                        conn.execute("""INSERT INTO movies (path,title,original_title,year,size,mtime,status,seen)
                                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                                title=excluded.title, original_title=excluded.original_title,
                                year=excluded.year,size=excluded.size,mtime=excluded.mtime,
                                status=excluded.status,seen=excluded.seen""",
                                     (rel, title, original, year, stat.st_size, stat.st_mtime, status, token))
                    except (OSError, ValueError):
                        continue
            if not STOP_EVENT.is_set():
                # Catalogue represents the latest selected folder only.
                conn.execute("DELETE FROM movies WHERE seen != ?", (token,))
                conn.commit()
            STATE["total"] = conn.execute("SELECT COUNT(*) FROM movies", ()).fetchone()[0]
            STATE["phase"] = "lookup"
            rows = conn.execute("SELECT path,title,original_title,year FROM movies WHERE status IN ('pending','error') ORDER BY title").fetchall()
            STATE["processed"] = STATE["total"] - len(rows)
            for row in rows:
                if STOP_EVENT.is_set():
                    raise InterruptedError("Stopped")
                try:
                    status, name, score = classify(conn, row["title"], row["original_title"], row["year"])
                    error = ""
                except InterruptedError:
                    raise
                except Exception as exc:
                    status, name, score, error = "error", "", 0.0, str(exc)[:300]
                    if "HTTP 401" in error or "HTTP 403" in error:
                        STATE["error"] = error
                        # Stop on invalid credentials instead of hammering API.
                        conn.execute("UPDATE movies SET status=?,error=?,checked_at=? WHERE path=?",
                                     (status, error, int(time.time()), row["path"]))
                        conn.commit()
                        STATE["phase"] = "error"
                        break
                conn.execute("""UPDATE movies SET status=?,match_title=?,score=?,error=?,checked_at=? WHERE path=?""",
                             (status, name, score, error, int(time.time()), row["path"]))
                conn.commit()
                STATE["processed"] += 1
        if STATE["phase"] != "error":
            STATE["phase"] = "done"
    except InterruptedError:
        STATE["phase"] = "stopped"
    except Exception as exc:
        STATE["phase"] = "error"
        STATE["error"] = str(exc)[:350]
    finally:
        STATE["running"] = False
        STATE["ended_at"] = datetime.now(timezone.utc).isoformat()


def rows_query(params):
    filters, values = [], []
    status = params.get("status", [""])[0]
    if status in ("found", "missing", "review", "error", "pending"):
        filters.append("status=?")
        values.append(status)
    search = params.get("q", [""])[0].strip()[:150]
    if search:
        filters.append("(title LIKE ? OR path LIKE ? OR match_title LIKE ?)")
        values.extend([f"%{search}%"] * 3)
    return ("WHERE " + " AND ".join(filters) if filters else ""), values


class Handler(BaseHTTPRequestHandler):
    server_version = "C411LibraryChecker/0.1"

    def log_message(self, format, *args):
        # Do not accidentally log secrets provided in URL/query strings.
        if os.environ.get("DEBUG", "0") == "1":
            print("HTTP", self.command, urlsplit(self.path).path, format % args, flush=True)

    def auth(self):
        token = self.headers.get("Authorization", "")
        try:
            if token.startswith("Basic "):
                value = base64.b64decode(token[6:], validate=True).decode("utf-8")
                user, password = value.split(":", 1)
                if secrets.compare_digest(user, APP_USER) and secrets.compare_digest(password, APP_PASSWORD):
                    return True
        except (ValueError, UnicodeError, binascii.Error):
            pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="C411 Library Checker"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return False

    def respond(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            return self.respond({"ok": True})
        if not self.auth():
            return
        params = parse_qs(urlsplit(self.path).query)
        if path in ("/", "/app.js", "/style.css"):
            name = "index.html" if path == "/" else path.lstrip("/")
            file = Path(__file__).parent / "static" / name
            if not file.exists():
                return self.respond({"error": "Not found"}, 404)
            raw = file.read_bytes()
            mime = {"index.html": "text/html; charset=utf-8", "app.js": "application/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}[name]
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/api/status":
            with db() as conn:
                counts = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM movies GROUP BY status")}
            return self.respond({"job": STATE.copy(), "counts": counts, "configured": bool(API_KEY), "media_root": str(MEDIA_ROOT), "rate_interval": REQUEST_INTERVAL})
        if path == "/api/folders":
            folders = ["."]
            if MEDIA_ROOT.is_dir():
                for base, dirs, _ in os.walk(MEDIA_ROOT, followlinks=False):
                    depth = len(Path(base).relative_to(MEDIA_ROOT).parts)
                    dirs[:] = [d for d in sorted(dirs) if not d.startswith(".") and not (Path(base) / d).is_symlink()]
                    if depth >= 3:
                        dirs[:] = []
                    folders.extend(str((Path(base) / d).relative_to(MEDIA_ROOT)) for d in dirs)
                    if len(folders) > 1500:
                        break
            return self.respond({"folders": folders[:1500]})
        if path in ("/api/movies", "/api/export.csv"):
            clause, values = rows_query(params)
            with db() as conn:
                if path == "/api/movies":
                    limit = max(1, min(200, int(params.get("limit", ["50"])[0])))
                    offset = max(0, int(params.get("offset", ["0"])[0]))
                    total = conn.execute("SELECT COUNT(*) FROM movies " + clause, values).fetchone()[0]
                    results = conn.execute("SELECT path,title,original_title,year,size,status,match_title,score,error FROM movies " + clause + " ORDER BY title LIMIT ? OFFSET ?", values + [limit, offset]).fetchall()
                    return self.respond({"total": total, "items": [dict(row) for row in results]})
                rows = conn.execute("SELECT title,original_title,year,path,size,status,match_title,score,error FROM movies " + clause + " ORDER BY title", values)
                sio = io.StringIO()
                writer = csv.writer(sio)
                writer.writerow(["Title", "Original title", "Year", "Path", "Size bytes", "Status", "C411 candidate", "Confidence", "Error"])
                for row in rows:
                    # Formula injection mitigation when opened in spreadsheet software.
                    writer.writerow([("'" + str(x) if isinstance(x, str) and x.startswith(("=", "+", "-", "@")) else x) for x in row])
                raw = sio.getvalue().encode("utf-8-sig")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="c411-library-checker.csv"')
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
                return
        return self.respond({"error": "Not found"}, 404)

    def do_POST(self):
        if not self.auth():
            return
        path = urlsplit(self.path).path
        size = int(self.headers.get("Content-Length", "0"))
        if size > 4096:
            return self.respond({"error": "Payload too large"}, 413)
        try:
            payload = json.loads(self.rfile.read(size) or b"{}")
        except (ValueError, UnicodeError, binascii.Error):
            return self.respond({"error": "Invalid JSON"}, 400)
        if path == "/api/scan":
            folder, refresh = payload.get("folder", "."), bool(payload.get("refresh", False))
            try:
                safe_directory(folder)
            except ValueError as exc:
                return self.respond({"error": str(exc)}, 400)
            if not API_KEY:
                return self.respond({"error": "Configure C411_API_KEY in .env"}, 400)
            with WORK_LOCK:
                if STATE["running"]:
                    return self.respond({"error": "Scan already running"}, 409)
                STOP_EVENT.clear()
                STATE["running"] = True
                worker = threading.Thread(target=run_scan, args=(folder, refresh), daemon=True)
                WORKER[0] = worker
                worker.start()
            return self.respond({"ok": True}, 202)
        if path == "/api/stop":
            STOP_EVENT.set()
            return self.respond({"ok": True})
        return self.respond({"error": "Not found"}, 404)


def main():
    if not APP_PASSWORD or APP_PASSWORD in ("change-this-password", "CHANGE_ME"):
        raise SystemExit("Set a strong APP_PASSWORD in .env before starting.")
    # Only permit official C411 HTTPS endpoint, avoiding accidental API-key leakage.
    target = urlsplit(API_URL)
    if target.scheme != "https" or target.hostname != "c411.org" or target.path != "/api/torznab":
        raise SystemExit("C411_API_URL must be https://c411.org/api/torznab")
    initialize()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"C411 Library Checker running on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
