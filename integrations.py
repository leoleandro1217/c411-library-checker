"""Optional, server-side integrations. No media file is ever modified here."""
from __future__ import annotations

import json
import os
from http.cookiejar import CookieJar
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

JELLYFIN_URL = os.environ.get('JELLYFIN_URL', '').rstrip('/')
JELLYFIN_API_KEY = os.environ.get('JELLYFIN_API_KEY', '')
JELLYFIN_USER_ID = os.environ.get('JELLYFIN_USER_ID', '')
JELLYFIN_PATH_PREFIX = os.environ.get('JELLYFIN_PATH_PREFIX', '')
QBIT_URL = os.environ.get('QBIT_URL', '').rstrip('/')
QBIT_API_KEY = os.environ.get('QBIT_API_KEY', '')
QBIT_USERNAME = os.environ.get('QBIT_USERNAME', '')
QBIT_PASSWORD = os.environ.get('QBIT_PASSWORD', '')
CROSS_SEED_URL = os.environ.get('CROSS_SEED_URL', '').rstrip('/')
CROSS_SEED_API_KEY = os.environ.get('CROSS_SEED_API_KEY', '')
MAX_RESPONSE = 12 * 1024 * 1024


def base_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('Invalid integration URL: expected http(s)://host:port')
    return url


def read_response(response):
    blob = response.read(MAX_RESPONSE + 1)
    if len(blob) > MAX_RESPONSE:
        raise ValueError('Integration response too large')
    return blob


def jellyfin_movies():
    """Fetch movies from Jellyfin's /Items endpoint, paginating large libraries."""
    if not (JELLYFIN_URL and JELLYFIN_API_KEY):
        raise ValueError('Jellyfin URL and API key not configured')
    base_url(JELLYFIN_URL)
    items, start = [], 0
    endpoint = f'/Users/{JELLYFIN_USER_ID}/Items' if JELLYFIN_USER_ID else '/Items'
    for _ in range(200):  # up to 100,000 movies
        params = urlencode({'Recursive': 'true', 'IncludeItemTypes': 'Movie', 'Fields': 'Path,ProviderIds', 'StartIndex': start, 'Limit': 500})
        request = Request(JELLYFIN_URL + endpoint + '?' + params,
                          headers={'X-Emby-Token': JELLYFIN_API_KEY, 'Accept': 'application/json'})
        with urlopen(request, timeout=25) as response:
            result = json.loads(read_response(response))
        page = result.get('Items') or []
        items.extend(page)
        start += len(page)
        if not page or start >= result.get('TotalRecordCount', start):
            return items
    raise ValueError('Too many Jellyfin pages; aborted')


def jellyfin_relative_path(path, prefix):
    """Avoid accidental basename-only matching; require an explicit container path mapping."""
    from pathlib import PurePosixPath
    if not prefix or not path or not path.startswith('/') or not prefix.startswith('/'):
        return None
    full = PurePosixPath(path)
    root = PurePosixPath(prefix)
    try:
        rel = full.relative_to(root)
    except ValueError:
        return None
    if str(rel) == '.':
        return None
    return rel.as_posix()


def qbittorrent_torrents():
    if not QBIT_URL:
        raise ValueError('qBittorrent URL not configured')
    base_url(QBIT_URL)
    headers = {'Accept': 'application/json'}
    if QBIT_API_KEY:
        # v5.2+ supports Bearer tokens.
        headers['Authorization'] = 'Bearer ' + QBIT_API_KEY
        request = Request(QBIT_URL + '/api/v2/torrents/info', headers=headers)
        with urlopen(request, timeout=20) as response:
            torrents = json.loads(read_response(response))
    else:
        if not QBIT_USERNAME or not QBIT_PASSWORD:
            raise ValueError('qBittorrent API key or username/password required')
        # qBit 4.x/5.x cookie-based auth. Cookie retained only in this request.
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        login = Request(QBIT_URL + '/api/v2/auth/login',
                        data=urlencode({'username': QBIT_USERNAME, 'password': QBIT_PASSWORD}).encode('utf-8'),
                        headers={'Referer': QBIT_URL, 'Content-Type': 'application/x-www-form-urlencoded'})
        with opener.open(login, timeout=20) as response:
            if read_response(response).strip() != b'Ok.':
                raise ValueError('qBittorrent login rejected')
        with opener.open(Request(QBIT_URL + '/api/v2/torrents/info', headers={'Referer': QBIT_URL}), timeout=20) as response:
            torrents = json.loads(read_response(response))
    if not isinstance(torrents, list):
        raise ValueError('Unexpected qBittorrent reply')
    # Never return tracker URLs, passwords, passkeys, peers or announce information.
    return [{'name': t.get('name', ''), 'hash': t.get('hash', ''), 'size': t.get('size', 0),
             'progress': t.get('progress', 0), 'state': t.get('state', ''),
             'upspeed': t.get('upspeed', 0), 'uploaded': t.get('uploaded', 0),
             'ratio': t.get('ratio', 0)} for t in torrents]


def cross_seed_ping():
    if not CROSS_SEED_URL:
        raise ValueError('cross-seed URL not configured')
    base_url(CROSS_SEED_URL)
    with urlopen(Request(CROSS_SEED_URL + '/api/ping'), timeout=10) as response:
        read_response(response)
        return response.status == 200


def cross_seed_trigger_search():
    """Triggers cross-seed's own matching job; it alone decides safe matches/injection."""
    if not CROSS_SEED_URL or not CROSS_SEED_API_KEY:
        raise ValueError('cross-seed URL or API key not configured')
    base_url(CROSS_SEED_URL)
    payload = json.dumps({'name': 'search'}).encode('utf-8')
    request = Request(CROSS_SEED_URL + '/api/job', method='POST', data=payload,
                      headers={'Content-Type': 'application/json', 'X-Api-Key': CROSS_SEED_API_KEY})
    with urlopen(request, timeout=25) as response:
        read_response(response)
        return response.status in (200, 202, 204)
