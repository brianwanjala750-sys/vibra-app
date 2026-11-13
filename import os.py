import os
import json
import mimetypes
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from flask import Flask, Response, jsonify, request, send_from_directory, session
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

DATA_DIR = os.environ.get('VIBRA_DATA_DIR', os.path.join(os.path.dirname(__file__), 'instance'))
DATABASE_PATH = os.path.join(DATA_DIR, 'vibra.sqlite3')


def _session_secret():
    configured_secret = os.environ.get('VIBRA_SECRET_KEY')
    if configured_secret:
        return configured_secret
    os.makedirs(DATA_DIR, exist_ok=True)
    secret_path = os.path.join(DATA_DIR, 'session.key')
    try:
        descriptor = os.open(secret_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        with open(secret_path, 'r', encoding='utf-8') as secret_file:
            secret = secret_file.read().strip()
        if not secret:
            raise RuntimeError('The VIBRA session secret file is empty.')
        return secret
    secret = secrets.token_urlsafe(48)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as secret_file:
        secret_file.write(secret)
    return secret


app = Flask(__name__, static_folder=None)
app.config.update(
    SECRET_KEY=_session_secret(),
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('VIBRA_COOKIE_SECURE') == '1',
)
if os.environ.get('TRUST_PROXY') == '1':
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri=os.environ.get('VIBRA_RATE_LIMIT_STORAGE_URL', 'memory://'),
)


@app.errorhandler(429)
def handle_rate_limit(error):
    return jsonify({'status': 'error', 'message': 'Too many requests. Please try again shortly.'}), 429

ARCHIVE_API = 'https://archive.org'
ALLOWED_LICENSES = {
    'http://creativecommons.org/publicdomain/mark/1.0/': 'Public Domain',
    'http://creativecommons.org/publicdomain/zero/1.0/': 'CC0',
    'http://creativecommons.org/licenses/by/4.0/': 'CC BY 4.0',
    'http://creativecommons.org/licenses/by-sa/4.0/': 'CC BY-SA 4.0',
}
MEDIA_EXTENSIONS = {
    'audio': ('.mp3', '.ogg', '.wav', '.m4a', '.flac', '.aac'),
    'movies': ('.mp4', '.webm', '.m4v', '.mov', '.ogv'),
}
CATALOG_CACHE = {}
METADATA_CACHE = {}


def _archive_json(url):
    req = Request(url, headers={'User-Agent': 'VIBRA/1.0 (open media catalog)'})
    with urlopen(req, timeout=15) as response:
        return json.load(response)


def _item_metadata(identifier):
    now = time.monotonic()
    cached = METADATA_CACHE.get(identifier)
    if cached and cached[0] > now:
        return cached[1]
    url = f'{ARCHIVE_API}/metadata/{quote(identifier, safe="")}'
    result = _archive_json(url)
    METADATA_CACHE[identifier] = (now + 300, result)
    return result


def _media_file(metadata, media_type):
    accepted = MEDIA_EXTENSIONS[media_type]
    for file_info in metadata.get('files', []):
        name = file_info.get('name', '')
        if (
            file_info.get('source') == 'original'
            and name.lower().endswith(accepted)
            and '/' not in name
            and '\\' not in name
        ):
            return name
    return None


def _is_twitch_upload(metadata):
    title = metadata.get('title', '')
    return isinstance(title, str) and re.search(r'\btwitch\b', title, re.IGNORECASE) is not None


def _catalog_item(doc, media_type):
    identifier = doc.get('identifier')
    if not identifier or not re.fullmatch(r'[A-Za-z0-9._-]{1,200}', identifier):
        return None
    metadata = _item_metadata(identifier)
    item_meta = metadata.get('metadata', {})
    license_url = item_meta.get('licenseurl')
    if (
        item_meta.get('mediatype') != media_type
        or license_url not in ALLOWED_LICENSES
        or (media_type == 'movies' and _is_twitch_upload(item_meta))
    ):
        return None
    filename = _media_file(metadata, media_type)
    if not filename:
        return None
    media_kind = 'Music' if media_type == 'audio' else 'Videos'
    encoded_id = quote(identifier, safe='')
    encoded_file = quote(filename, safe='')
    return {
        'id': identifier,
        'identifier': identifier,
        'title': item_meta.get('title') or doc.get('title') or identifier,
        'artist': item_meta.get('creator') or doc.get('creator') or 'Internet Archive',
        'type': media_kind,
        'img': f'{ARCHIVE_API}/services/img/{encoded_id}',
        'streamUrl': f'{ARCHIVE_API}/download/{encoded_id}/{encoded_file}',
        'downloadUrl': f'/api/catalog/{encoded_id}/download?file={encoded_file}',
        'fileName': filename,
        'licenseUrl': license_url,
        'licenseName': ALLOWED_LICENSES[license_url],
        'detailsUrl': f'{ARCHIVE_API}/details/{encoded_id}',
        'publicdate': item_meta.get('publicdate', ''),
        'local': False,
    }


def _search_catalog(media_type, query, page):
    clauses = ' OR '.join(f'licenseurl:"{license}"' for license in ALLOWED_LICENSES)
    archive_type = 'audio' if media_type == 'audio' else 'movies'
    search_query = f'mediatype:{archive_type} AND ({clauses})'
    if media_type == 'audio':
        search_query += ' AND subject:music'
    if query:
        escaped_query = query.replace('\\', '\\\\').replace('"', '\\"')
        search_query += f' AND title:"{escaped_query}"'
    params = [
        ('q', search_query),
        ('fl[]', 'identifier'),
        ('fl[]', 'title'),
        ('fl[]', 'creator'),
        ('rows', '24'),
        ('page', str(page)),
        ('sort[]', 'publicdate desc'),
        ('output', 'json'),
    ]
    response = _archive_json(f'{ARCHIVE_API}/advancedsearch.php?{urlencode(params)}')
    docs = response.get('response', {}).get('docs', [])
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda doc: _catalog_item(doc, media_type), docs))
    return [item for item in results if item]


def _cached_catalog(media_type, query, page):
    key = (media_type, query, page)
    now = time.monotonic()
    cached = CATALOG_CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]
    results = _search_catalog(media_type, query, page)
    CATALOG_CACHE[key] = (now + 300, results)
    return results


def _session_uid():
    user_id = session.get('user_id')
    if not user_id:
        return None, (jsonify({'status': 'error', 'message': 'Sign in is required.'}), 401)
    return user_id, None


@contextmanager
def _database_connection():
    connection = sqlite3.connect(DATABASE_PATH)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _initialize_database():
    os.makedirs(DATA_DIR, exist_ok=True)
    with _database_connection() as connection:
        connection.execute(
            'CREATE TABLE IF NOT EXISTS users ('
            'id TEXT PRIMARY KEY, email TEXT NOT NULL COLLATE NOCASE UNIQUE, '
            'password_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)'
        )
        connection.execute(
            'CREATE TABLE IF NOT EXISTS user_library ('
            'user_id TEXT NOT NULL, item_id TEXT NOT NULL, item_json TEXT NOT NULL, '
            'PRIMARY KEY (user_id, item_id))'
        )


_initialize_database()

@app.route('/')
def serve_index():
    return send_from_directory('.', 'VIBRA.html')

@app.route('/local-auth.js')
def serve_local_auth():
    return send_from_directory('.', 'local-auth.js', mimetype='application/javascript')

@app.route('/manifest.webmanifest')
def serve_manifest():
    return send_from_directory('.', 'manifest.webmanifest', mimetype='application/manifest+json')

@app.route('/service-worker.js')
def serve_service_worker():
    response = send_from_directory('.', 'service-worker.js', mimetype='application/javascript')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Service-Worker-Allowed'] = '/'
    return response

@app.route('/icons/vibra.svg')
def serve_app_icon():
    return send_from_directory('icons', 'vibra.svg', mimetype='image/svg+xml')

@app.route('/icons/vibra-192.png')
def serve_app_icon_192():
    return send_from_directory('icons', 'vibra-192.png', mimetype='image/png')

@app.route('/icons/vibra-512.png')
def serve_app_icon_512():
    return send_from_directory('icons', 'vibra-512.png', mimetype='image/png')

@app.route('/api/auth/session', methods=['GET'])
def get_auth_session():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'status': 'success', 'data': None})
    with _database_connection() as connection:
        row = connection.execute(
            'SELECT id, email FROM users WHERE id = ?',
            (user_id,),
        ).fetchone()
    if not row:
        session.clear()
        return jsonify({'status': 'success', 'data': None})
    return jsonify({'status': 'success', 'data': {'id': row[0], 'email': row[1]}})

@app.route('/api/auth/register', methods=['POST'])
@limiter.limit('5 per minute')
def register_account():
    data = request.get_json(silent=True) or {}
    email = data.get('email')
    password = data.get('password')
    if not isinstance(email, str):
        return jsonify({'status': 'error', 'message': 'Enter a valid email address.'}), 400
    email = email.strip().lower()
    if len(email) > 254 or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
        return jsonify({'status': 'error', 'message': 'Enter a valid email address.'}), 400
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        return jsonify({'status': 'error', 'message': 'Password must be 8 to 128 characters.'}), 400

    user_id = uuid.uuid4().hex
    password_hash = generate_password_hash(password)
    try:
        with _database_connection() as connection:
            connection.execute(
                'INSERT INTO users (id, email, password_hash) VALUES (?, ?, ?)',
                (user_id, email, password_hash),
            )
    except sqlite3.IntegrityError:
        return jsonify({'status': 'error', 'message': 'An account with that email already exists.'}), 409
    session.clear()
    session['user_id'] = user_id
    session.permanent = True
    return jsonify({'status': 'success', 'data': {'id': user_id, 'email': email}}), 201

@app.route('/api/auth/login', methods=['POST'])
@limiter.limit('10 per minute')
def login_account():
    data = request.get_json(silent=True) or {}
    email = data.get('email')
    password = data.get('password')
    if (
        not isinstance(email, str)
        or not isinstance(password, str)
        or len(email) > 254
        or len(password) > 128
    ):
        return jsonify({'status': 'error', 'message': 'Enter your email and password.'}), 400
    with _database_connection() as connection:
        row = connection.execute(
            'SELECT id, email, password_hash FROM users WHERE email = ?',
            (email.strip().lower(),),
        ).fetchone()
    if not row or not check_password_hash(row[2], password):
        return jsonify({'status': 'error', 'message': 'Email or password is incorrect.'}), 401
    session.clear()
    session['user_id'] = row[0]
    session.permanent = True
    return jsonify({'status': 'success', 'data': {'id': row[0], 'email': row[1]}})

@app.route('/api/auth/logout', methods=['POST'])
def logout_account():
    session.clear()
    return jsonify({'status': 'success', 'data': None})

@app.route('/api/catalog', methods=['GET'])
@limiter.limit('60 per minute')
def get_catalog():
    requested_type = request.args.get('type', 'all').lower()
    query = request.args.get('q', '').strip()[:100]
    try:
        page = max(1, min(int(request.args.get('page', '1')), 100))
    except ValueError:
        return jsonify({'status': 'error', 'message': 'Page must be a positive number.'}), 400
    if requested_type not in ('all', 'audio', 'video'):
        return jsonify({'status': 'error', 'message': 'Type must be audio, video, or all.'}), 400
    types = ('audio', 'video') if requested_type == 'all' else (requested_type,)
    try:
        with ThreadPoolExecutor(max_workers=len(types)) as pool:
            results = list(pool.map(
                lambda kind: _cached_catalog('audio' if kind == 'audio' else 'movies', query, page),
                types,
            ))
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        app.logger.exception('Internet Archive catalog request failed')
        return jsonify({'status': 'error', 'message': f'Could not load the licensed media catalog: {error}'}), 502
    items = [item for batch in results for item in batch]
    items.sort(key=lambda item: item['publicdate'], reverse=True)
    return jsonify({'status': 'success', 'data': items})

@app.route('/api/catalog/<identifier>/download', methods=['GET'])
def download_catalog_item(identifier):
    filename = request.args.get('file', '')
    if (
        not re.fullmatch(r'[A-Za-z0-9._-]{1,200}', identifier)
        or not filename
        or os.path.basename(filename) != filename
        or '\\' in filename
        or not filename.lower().endswith(MEDIA_EXTENSIONS['audio'] + MEDIA_EXTENSIONS['movies'])
    ):
        return jsonify({'status': 'error', 'message': 'Invalid media file.'}), 400
    try:
        metadata = _item_metadata(identifier)
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        app.logger.exception('Internet Archive metadata request failed')
        return jsonify({'status': 'error', 'message': f'Could not verify this media item: {error}'}), 502
    item_meta = metadata.get('metadata', {})
    if (
        item_meta.get('mediatype') not in MEDIA_EXTENSIONS
        or item_meta.get('licenseurl') not in ALLOWED_LICENSES
        or (item_meta.get('mediatype') == 'movies' and _is_twitch_upload(item_meta))
        or filename not in (info.get('name') for info in metadata.get('files', []))
        or filename != _media_file(metadata, item_meta.get('mediatype', ''))
    ):
        return jsonify({'status': 'error', 'message': 'This file is not available for download.'}), 404
    source_url = f'{ARCHIVE_API}/download/{quote(identifier, safe="")}/{quote(filename, safe="")}'
    headers = {'User-Agent': 'VIBRA/1.0 (open media catalog)'}
    range_header = request.headers.get('Range')
    if range_header:
        headers['Range'] = range_header
    try:
        upstream = urlopen(Request(source_url, headers=headers), timeout=30)
    except HTTPError as error:
        if error.code == 416:
            return Response(status=416, headers={'Content-Range': error.headers.get('Content-Range', '')})
        app.logger.exception('Internet Archive media download failed')
        return jsonify({'status': 'error', 'message': 'The media provider could not serve this file.'}), 502
    except (URLError, TimeoutError) as error:
        app.logger.exception('Internet Archive media download failed')
        return jsonify({'status': 'error', 'message': f'The media provider could not serve this file: {error}'}), 502

    def stream():
        try:
            while True:
                chunk = upstream.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()

    safe_filename = re.sub(r'[^A-Za-z0-9._-]', '_', filename)
    response_headers = {
        'Content-Type': mimetypes.guess_type(filename)[0] or 'application/octet-stream',
        'Content-Disposition': f"attachment; filename=\"{safe_filename}\"; filename*=UTF-8''{quote(filename, safe='')}",
        'Accept-Ranges': 'bytes',
    }
    for header in ('Content-Length', 'Content-Range'):
        if upstream.headers.get(header):
            response_headers[header] = upstream.headers[header]
    return Response(stream(), status=getattr(upstream, 'status', 200), headers=response_headers)

@app.route('/api/library', methods=['GET', 'POST', 'DELETE'])
def manage_library():
    user_id, auth_error = _session_uid()
    if auth_error:
        return auth_error
    if request.method == 'GET':
        with _database_connection() as connection:
            rows = connection.execute(
                'SELECT item_json FROM user_library WHERE user_id = ? ORDER BY rowid DESC',
                (user_id,),
            ).fetchall()
        return jsonify({'status': 'success', 'data': [json.loads(row[0]) for row in rows]})

    data = request.get_json(silent=True) or {}
    item = data.get('item')

    if request.method == 'POST':
        if not item or 'id' not in item:
            return jsonify({"status": "error", "message": "Item with id required"}), 400
        with _database_connection() as connection:
            connection.execute(
                'INSERT OR REPLACE INTO user_library (user_id, item_id, item_json) VALUES (?, ?, ?)',
                (user_id, str(item['id']), json.dumps(item)),
            )
            rows = connection.execute(
                'SELECT item_json FROM user_library WHERE user_id = ? ORDER BY rowid DESC',
                (user_id,),
            ).fetchall()
        return jsonify({"status": "success", "message": "Item saved successfully", "data": [json.loads(row[0]) for row in rows]})

    # DELETE
    if item and 'id' in item:
        with _database_connection() as connection:
            connection.execute(
                'DELETE FROM user_library WHERE user_id = ? AND item_id = ?',
                (user_id, str(item['id'])),
            )
            rows = connection.execute(
                'SELECT item_json FROM user_library WHERE user_id = ? ORDER BY rowid DESC',
                (user_id,),
            ).fetchall()
    else:
        with _database_connection() as connection:
            rows = connection.execute(
                'SELECT item_json FROM user_library WHERE user_id = ? ORDER BY rowid DESC',
                (user_id,),
            ).fetchall()
    return jsonify({"status": "success", "message": "Item removed", "data": [json.loads(row[0]) for row in rows]})

if __name__ == '__main__':
    from waitress import serve
    serve(
        app,
        host=os.environ.get('HOST', '127.0.0.1'),
        port=int(os.environ.get('PORT', '5000')),
        threads=8,
    )