"""
Vendrop — browser-based vendor archive drop-off, constrained to Nexus
raw repo paths the admin pre-configures per project.

Surfaces
--------
  /             admin login page
  /admin        admin dashboard (project list, audit log)
  /admin/new    create project
  /admin/p/<id> edit / regen token / delete project
  /u/<slug>     vendor drop page (token in URL: ?t=<token>)
  /u/<slug>/upload  POST stream to Nexus

Design choices
--------------
- Per-project vendor token in URL. Admin shares one bookmarkable link per
  vendor; rotatable from the admin UI. No vendor accounts to manage.
- Stream the file body straight to Nexus raw API; no local copy kept.
- Project config + audit log are JSON on disk so a PVC can survive restarts.
- OCP-friendly: random UID, GID 0, /app writable via group, /data is the PVC.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import quote as urlquote

import requests
from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template,
    request, session, url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

VERSION = '1.0.4'

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NEXUS_RAW_BASE = os.environ.get('NEXUS_RAW_BASE', '').rstrip('/')
if not NEXUS_RAW_BASE:
    raise SystemExit(
        "NEXUS_RAW_BASE is required (e.g. https://nexus.example.com)"
    )
NEXUS_USER       = os.environ.get('NEXUS_USER', 'admin')
NEXUS_PASS       = os.environ.get('NEXUS_PASS', '')
NEXUS_VERIFY_TLS = os.environ.get('NEXUS_VERIFY_TLS', 'true').lower() != 'false'

ADMIN_USER       = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASS       = os.environ.get('ADMIN_PASS', '')  # required, no default
SECRET_KEY       = os.environ.get('SECRET_KEY', secrets.token_hex(32))

DATA_DIR     = Path(os.environ.get('DATA_DIR', '/data'))
PROJECTS_FILE = DATA_DIR / 'projects.json'
AUDIT_FILE    = DATA_DIR / 'audit.jsonl'

MAX_AUDIT_DISPLAY = int(os.environ.get('MAX_AUDIT_DISPLAY', '200'))
DEFAULT_MAX_MB    = int(os.environ.get('DEFAULT_MAX_MB', '500'))

PORT    = int(os.environ.get('PORT', '8080'))
WORKERS = int(os.environ.get('WORKERS', '1'))   # currently unused, Flask dev server

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
log = logging.getLogger('vendrop')

# ---------------------------------------------------------------------------
# Data model — projects + audit, persisted to JSON
# ---------------------------------------------------------------------------

@dataclass
class Project:
    id: str                       # short random id, used as URL slug
    name: str                     # display name (e.g. "EPS")
    nexus_repo: str               # raw repo name in Nexus (e.g. "raw")
    nexus_path: str               # path prefix inside repo (e.g. "eps/")
    token: str                    # bearer token for vendor URL
    allowed_extensions: list[str] = field(default_factory=list)  # ["zip","tar","war"]
    max_mb: int = DEFAULT_MAX_MB
    created_at: str = ''
    created_by: str = ''


_lock = threading.Lock()


def _load_projects() -> dict[str, Project]:
    if not PROJECTS_FILE.exists():
        return {}
    with PROJECTS_FILE.open() as f:
        raw = json.load(f)
    return {pid: Project(**p) for pid, p in raw.items()}


def _save_projects(projects: dict[str, Project]) -> None:
    PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROJECTS_FILE.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump({pid: asdict(p) for pid, p in projects.items()}, f, indent=2)
    tmp.replace(PROJECTS_FILE)


def _append_audit(entry: dict) -> None:
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_FILE.open('a') as f:
        f.write(json.dumps(entry) + '\n')


def _read_audit(limit: int = MAX_AUDIT_DISPLAY) -> list[dict]:
    if not AUDIT_FILE.exists():
        return []
    with AUDIT_FILE.open() as f:
        lines = f.readlines()
    out = []
    for line in lines[-limit:][::-1]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r'^[a-z0-9-]{3,32}$')
_FILENAME_SAFE = re.compile(r'^[A-Za-z0-9._-]+$')


def _new_id() -> str:
    """Short URL-safe id, ~7 chars."""
    return secrets.token_urlsafe(5).lower().replace('_', '').replace('-', '')[:7]


def _new_token() -> str:
    return secrets.token_urlsafe(24)


def _normalise_path(path: str) -> str:
    """Strip leading slash, ensure trailing slash, collapse ..; reject absolute paths."""
    if not path:
        return ''
    path = path.strip().lstrip('/')
    if '..' in path.split('/'):
        raise ValueError('path traversal not allowed')
    if path and not path.endswith('/'):
        path += '/'
    return path


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)
# Honor X-Forwarded-* from the OCP Route so request.host_url returns https://
# (Route does TLS edge termination — pod sees plain HTTP otherwise.)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
app.secret_key = SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = None  # we enforce per-project limits ourselves


@app.before_request
def _load_state():
    g.projects = _load_projects()


def _is_admin() -> bool:
    return session.get('admin') is True


def _require_admin():
    if not _is_admin():
        abort(401)


def _get_project_or_404(pid: str) -> Project:
    p = g.projects.get(pid)
    if not p:
        abort(404)
    return p


def _check_token(project: Project, supplied: str | None) -> bool:
    if not supplied:
        return False
    return hmac.compare_digest(project.token, supplied)

# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@app.route('/', methods=['GET', 'POST'])
def login():
    if _is_admin():
        return redirect(url_for('admin'))
    error = None
    if request.method == 'POST':
        u = request.form.get('username', '')
        p = request.form.get('password', '')
        if not ADMIN_PASS:
            error = 'ADMIN_PASS not configured on server.'
        elif u == ADMIN_USER and hmac.compare_digest(p, ADMIN_PASS):
            session['admin'] = True
            return redirect(url_for('admin'))
        else:
            error = 'Invalid credentials.'
    return render_template('login.html', error=error, version=VERSION)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/admin')
def admin():
    _require_admin()
    projects = sorted(g.projects.values(), key=lambda p: p.name.lower())
    audit = _read_audit()
    return render_template(
        'admin.html',
        projects=projects,
        audit=audit,
        nexus_raw_base=NEXUS_RAW_BASE,
        public_base=request.host_url.rstrip('/'),
        version=VERSION,
    )


@app.route('/admin/new', methods=['GET', 'POST'])
def admin_new():
    _require_admin()
    error = None
    if request.method == 'POST':
        try:
            name      = request.form['name'].strip()
            repo      = request.form['nexus_repo'].strip()
            path      = _normalise_path(request.form.get('nexus_path', ''))
            exts_raw  = request.form.get('allowed_extensions', '').strip()
            exts      = [e.strip().lstrip('.').lower()
                         for e in re.split(r'[\s,]+', exts_raw) if e.strip()]
            max_mb    = int(request.form.get('max_mb') or DEFAULT_MAX_MB)

            if not name or not repo:
                raise ValueError('name and Nexus repo are required')

            pid = _new_id()
            with _lock:
                projects = _load_projects()
                while pid in projects:
                    pid = _new_id()
                projects[pid] = Project(
                    id=pid, name=name, nexus_repo=repo, nexus_path=path,
                    token=_new_token(), allowed_extensions=exts, max_mb=max_mb,
                    created_at=_now_iso(), created_by=ADMIN_USER,
                )
                _save_projects(projects)
            flash(f"Created project '{name}'.")
            return redirect(url_for('admin_project', pid=pid))
        except (ValueError, KeyError) as exc:
            error = str(exc)
    return render_template('project_form.html', project=None, error=error, version=VERSION)


@app.route('/admin/p/<pid>', methods=['GET', 'POST'])
def admin_project(pid):
    _require_admin()
    p = _get_project_or_404(pid)
    error = None
    if request.method == 'POST':
        action = request.form.get('action', 'save')
        try:
            if action == 'delete':
                with _lock:
                    projects = _load_projects()
                    projects.pop(pid, None)
                    _save_projects(projects)
                flash(f"Deleted '{p.name}'.")
                return redirect(url_for('admin'))
            if action == 'regen_token':
                with _lock:
                    projects = _load_projects()
                    projects[pid].token = _new_token()
                    _save_projects(projects)
                flash("Token regenerated.")
                return redirect(url_for('admin_project', pid=pid))
            # default: save
            p.name      = request.form['name'].strip()
            p.nexus_repo = request.form['nexus_repo'].strip()
            p.nexus_path = _normalise_path(request.form.get('nexus_path', ''))
            exts_raw    = request.form.get('allowed_extensions', '').strip()
            p.allowed_extensions = [
                e.strip().lstrip('.').lower()
                for e in re.split(r'[\s,]+', exts_raw) if e.strip()
            ]
            p.max_mb = int(request.form.get('max_mb') or DEFAULT_MAX_MB)
            with _lock:
                projects = _load_projects()
                projects[pid] = p
                _save_projects(projects)
            flash("Saved.")
            return redirect(url_for('admin_project', pid=pid))
        except (ValueError, KeyError) as exc:
            error = str(exc)
    return render_template(
        'project_form.html',
        project=p,
        error=error,
        public_base=request.host_url.rstrip('/'),
        nexus_raw_base=NEXUS_RAW_BASE,
        version=VERSION,
    )

# ---------------------------------------------------------------------------
# Vendor routes
# ---------------------------------------------------------------------------

@app.route('/u/<pid>')
def vendor_page(pid):
    p = _get_project_or_404(pid)
    if not _check_token(p, request.args.get('t')):
        abort(403)
    return render_template(
        'vendor.html',
        project=p,
        token=request.args.get('t', ''),
        nexus_raw_base=NEXUS_RAW_BASE,
        version=VERSION,
    )


@app.route('/u/<pid>/upload', methods=['POST'])
def vendor_upload(pid):
    p = _get_project_or_404(pid)
    if not _check_token(p, request.args.get('t')):
        abort(403)

    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': 'no file in upload'}), 400

    filename = os.path.basename(f.filename)
    if not _FILENAME_SAFE.match(filename):
        return jsonify({
            'ok': False,
            'error': 'filename must match [A-Za-z0-9._-]+ (no spaces or unicode)',
        }), 400

    if p.allowed_extensions:
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        if ext not in p.allowed_extensions:
            return jsonify({
                'ok': False,
                'error': f'extension .{ext} not allowed (allowed: '
                         f'{", ".join(p.allowed_extensions)})',
            }), 400

    # Build destination URL — repo + path prefix is admin-controlled, filename
    # is sanity-checked above; quoting filename for safety regardless.
    dest = (
        f"{NEXUS_RAW_BASE}/repository/{p.nexus_repo}/"
        f"{p.nexus_path}{urlquote(filename)}"
    )

    # Stream the request body to Nexus while hashing on the way through.
    max_bytes = p.max_mb * 1024 * 1024
    sha = hashlib.sha256()
    total = [0]
    exceeded = [False]

    def gen() -> Iterator[bytes]:
        while True:
            chunk = f.stream.read(1024 * 1024)
            if not chunk:
                break
            total[0] += len(chunk)
            if total[0] > max_bytes:
                exceeded[0] = True
                return
            sha.update(chunk)
            yield chunk

    started = time.time()
    try:
        resp = requests.put(
            dest,
            data=gen(),
            auth=(NEXUS_USER, NEXUS_PASS),
            verify=NEXUS_VERIFY_TLS,
            timeout=(10, 600),
        )
    except requests.RequestException as exc:
        log.exception("nexus upload failed for %s", filename)
        return jsonify({'ok': False, 'error': f'nexus error: {exc}'}), 502

    if exceeded[0]:
        return jsonify({
            'ok': False,
            'error': f'file exceeded max_mb={p.max_mb} (stream aborted)',
        }), 413

    if not (200 <= resp.status_code < 300):
        log.warning("nexus rejected upload: %s %s", resp.status_code, resp.text[:500])
        return jsonify({
            'ok': False,
            'error': f'nexus returned HTTP {resp.status_code}',
            'detail': resp.text[:500],
        }), 502

    duration = time.time() - started
    digest = sha.hexdigest()
    entry = {
        'ts': _now_iso(),
        'project_id': p.id,
        'project_name': p.name,
        'filename': filename,
        'bytes': total[0],
        'sha256': digest,
        'dest': dest,
        'remote_addr': request.headers.get('X-Forwarded-For', request.remote_addr or ''),
        'user_agent': request.headers.get('User-Agent', '')[:200],
        'duration_s': round(duration, 2),
    }
    _append_audit(entry)

    return jsonify({
        'ok': True,
        'filename': filename,
        'bytes': total[0],
        'sha256': digest,
        'dest': dest,
        'duration_s': entry['duration_s'],
    })

# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.route('/health')
def health():
    return jsonify({
        'ok': True,
        'version': VERSION,
        'projects': len(g.projects),
        'nexus_base': NEXUS_RAW_BASE,
    })


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not ADMIN_PASS:
        log.warning("ADMIN_PASS is empty — admin login will be refused")
    log.info("vendrop %s listening on :%s  (data=%s, nexus=%s)",
             VERSION, PORT, DATA_DIR, NEXUS_RAW_BASE)
    app.run(host='0.0.0.0', port=PORT, threaded=True)
