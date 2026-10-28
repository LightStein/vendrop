# Vendrop

Browser-based upload tool that lets vendors drop archives into a Nexus raw
repository without giving them Nexus credentials, console access, or visibility into
anyone else's path. An admin defines the project and Nexus path;
vendor gets a tokenised URL and a drag-and-drop page.

The problem this solves: vendors hand-deliver builds (WAR, ZIP, tarballs)
and we have nowhere good to put them. Email-sized attachments don't fit,
S3 / SFTP needs new accounts per vendor, and giving them Nexus logins
pollutes the registry. Vendrop is a thin gate in front of the Nexus raw
API: admin-curated paths in, vendor uploads out, full audit trail.

---

## Architecture

```
   vendor browser                 vendrop pod                            Nexus raw
   ┌────────────┐   HTTPS    ┌───────────────────────────────┐  HTTPS  ┌──────────┐
   │ /u/<pid>?t │─────────▶  │ Flask + Werkzeug ProxyFix      │ ──────▶ │  /repo/  │
   │            │            │  • token check                  │   PUT   │  <path>/ │
   │ drag/drop  │            │  • extension + size check       │         │  <file>  │
   └────────────┘            │  • stream PUT to Nexus          │         └──────────┘
                             │  • append audit.jsonl           │
                             │                                 │
                             │  /data  ──▶ PVC (RWX, 1Gi)      │
                             │    projects.json (state)        │
                             │    audit.jsonl  (history)       │
                             └───────────────────────────────┘
                                          ▲
                                          │ admin (env-configured user)
                                          │ /admin  create/edit projects
                                          └─── rotate tokens, view audit
```

Single Flask process. No database — projects + audit live in JSON on a
PVC. Single replica with `Recreate` strategy; sharded state and concurrent
writes were not worth the complexity for the expected load.

Vendor uploads stream straight through the pod to Nexus (no spool to disk),
so memory stays flat regardless of file size. Per-project size cap is
enforced in the streaming loop and aborts mid-upload if exceeded.

---

## Components

| Path                              | Purpose                                              |
| --------------------------------- | ---------------------------------------------------- |
| `app.py`                          | Flask app: admin CRUD, vendor upload, audit          |
| `templates/`                      | Jinja2 — login, admin dashboard, project form, drop  |
| `requirements.txt`                | `flask`, `requests`                                  |
| `Dockerfile`                      | `python:3.11-alpine`, OCP random-UID safe            |
| `deployment.yaml`                 | Standalone manifest, no Helm needed                  |
| `chart/`                          | Helm chart (canonical deploy)                        |
| `chart/values.yaml`               | Per-env image / config / resources                   |

---

## Configuration

All config is env-based. Non-secret values come from the chart's `config`
block (rendered into a `ConfigMap`); secrets come from a `vendrop-secrets`
`Secret` created out-of-band.

| Variable           | Source     | Default                                                          | Notes |
| ------------------ | ---------- | ---------------------------------------------------------------- | ----- |
| `NEXUS_RAW_BASE`   | ConfigMap  | **required**, e.g. `https://nexus.example.com`                   | Trailing slash stripped; the app will not start without it |
| `NEXUS_VERIFY_TLS` | ConfigMap  | `true`                                                           | `false` only for a self-signed Nexus |
| `DATA_DIR`         | ConfigMap  | `/data`                                                          | PVC mount point |
| `DEFAULT_MAX_MB`   | ConfigMap  | `500`                                                            | Per-project cap on new projects |
| `MAX_AUDIT_DISPLAY`| ConfigMap  | `200`                                                            | Audit lines shown in dashboard |
| `PORT`             | ConfigMap  | `8080`                                                           | Container port |
| `NEXUS_USER`       | **Secret** | —                                                                | Nexus account used for raw PUT |
| `NEXUS_PASS`       | **Secret** | —                                                                | "" |
| `ADMIN_USER`       | **Secret** | —                                                                | UI login |
| `ADMIN_PASS`       | **Secret** | —                                                                | UI login |
| `SECRET_KEY`       | **Secret** | random per-process if unset                                      | Flask session signing; **must be set** in prod or sessions reset on restart |

---

## HTTP surface

| Method | Path                  | Auth      | Purpose                              |
| ------ | --------------------- | --------- | ------------------------------------ |
| GET/POST | `/`                 | —         | Login (admin)                        |
| GET    | `/logout`             | admin     | Drop session                         |
| GET    | `/admin`              | admin     | Dashboard: projects + recent audit   |
| GET/POST | `/admin/new`        | admin     | Create project                       |
| GET/POST | `/admin/p/<pid>`    | admin     | Edit / delete / rotate token         |
| GET    | `/u/<pid>?t=<token>`  | token     | Vendor drop page                     |
| POST   | `/u/<pid>/upload?t=…` | token     | Vendor upload (streaming PUT)        |
| GET    | `/health`             | —         | Liveness probe (`200 OK`)            |

---

## Deploy

The published image is `docker.io/nautilus444/vendrop` (MIT, built from this
repo). Mirror it into your own registry if the cluster cannot reach Docker Hub.

### Helm (recommended)

The chart is in `chart/`. Two values have no default and the render fails
without them, on purpose:

- `persistence.storageClassName` - an RWX class that needs no volume detach
  (CephFS, NFS, ...). Left to the cluster default, an in-tree provisioner can
  pin the pod to a node when a detach hangs.
- `<env>.config.NEXUS_RAW_BASE` - where every upload goes.

```bash
kubectl create namespace vendrop

kubectl create secret generic vendrop-secrets -n vendrop \
  --from-literal=NEXUS_USER='<nexus user that may write the raw repos>' \
  --from-literal=NEXUS_PASS='<password>' \
  --from-literal=ADMIN_USER=admin \
  --from-literal=ADMIN_PASS='<admin UI password>' \
  --from-literal=SECRET_KEY="$(openssl rand -hex 32)"

helm install vendrop ./chart -n vendrop \
  --set env=dev \
  --set dev_hostname=apps.example.com \
  --set dev.config.NEXUS_RAW_BASE=https://nexus.example.com \
  --set persistence.storageClassName=<your-rwx-class>
```

`env` selects one of the per-env blocks in `values.yaml` (preview, dev, uat,
prod, drc), which is handy for a GitOps repo that renders one release per
environment. For a single install, `dev` is fine.

The chart ships an OpenShift `Route` with `tls.termination: edge` and
`insecureEdgeTerminationPolicy: Redirect`. The app has `ProxyFix` wired, so
`request.host_url` returns `https://` and generated vendor links land on HTTPS
directly. On plain Kubernetes, put an Ingress in front of the `vendrop`
Service instead.

### Without Helm

`deployment.yaml` is a standalone manifest. Fill in every value marked
`CHANGE-ME`, then `kubectl apply -f deployment.yaml`.

### Building

```bash
docker build -t <registry>/vendrop:vX.Y.Z .
docker push  <registry>/vendrop:vX.Y.Z
```

Runs as an arbitrary UID in group 0, so it works under OpenShift's
`restricted` SCC as well as on plain Kubernetes.

---

## Admin guide

1. Open the Route or Ingress URL.
2. Sign in with `ADMIN_USER` / `ADMIN_PASS` (from the `vendrop-secrets`).
3. **Create project** — `/admin/new`. Fields:
   - **Name**: display only (e.g. "Acme").
   - **Nexus repo**: existing raw repo name (e.g. `raw`).
   - **Path prefix**: where uploads land inside the repo (e.g. `acme/`).
     Must end with `/`; `..` is rejected at save time.
   - **Allowed extensions**: comma-separated, blank = any. (e.g. `zip,war,tar`).
   - **Max size (MB)**: per-file cap.
4. Page shows the vendor URL — copy it, send to vendor via your preferred
   channel. URL contains the token; **anyone with this URL can upload**, so
   treat it as a credential.
5. To rotate the token, open the project page and click "Rotate token".
   The old URL stops working immediately.
6. The dashboard shows the latest 200 uploads (timestamp, project,
   filename, bytes, sha256, remote addr, duration).

---

## Vendor guide

Vendor receives a URL like `https://vendrop.example.com/u/abc123?t=...`.
They open it, drag a file onto the page, wait for the green tick.
Failures show the reason inline (wrong extension, too big, Nexus 4xx/5xx).
That is the entire vendor-facing surface.

---

## Operations

### Audit log

Append-only JSONL at `/data/audit.jsonl`. One line per successful upload:

```json
{"ts":"2026-05-13T09:42:11Z","project":"abc123","name":"app.war",
 "bytes":12345678,"sha256":"...","dest":"https://.../raw/acme/app.war",
 "remote":"10.x.x.x","ua":"Mozilla/...","duration_ms":842}
```

To grep:

```bash
kubectl exec -n vendrop deploy/vendrop -- cat /data/audit.jsonl \
  | jq 'select(.project=="abc123")'
```

### Backup

Two files on the PVC need a backup if you don't want to recreate state by
hand:

- `/data/projects.json` — project definitions + tokens.
- `/data/audit.jsonl`   — upload history.

A nightly `kubectl cp` to an external bucket is enough; both files
are flat and human-readable.

### Troubleshooting

| Symptom                                       | Likely cause / fix                                                                                                            |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Vendor URL shows "canceled" in browser        | App built `http://` URL but Route forces `https://`. **Pre-v1.0.1 bug** — bump to ≥ v1.0.1 (ProxyFix wired up).               |
| `401 Unauthorized` on upload                  | Vendor token revoked / project deleted. Rotate token and reissue URL.                                                          |
| `413 Request Entity Too Large`                | File exceeds project's `max_mb`. Raise the cap on the project page or split the upload.                                        |
| `502/504` to Nexus                            | Check `NEXUS_RAW_BASE` reachable from the pod, and that `NEXUS_USER`/`NEXUS_PASS` still valid.                                 |
| Admin can't log in after pod restart          | `SECRET_KEY` wasn't set, so sessions are signed with a per-process random key. Set `SECRET_KEY` in `vendrop-secrets`.          |
| Random UID can't write `/data`                | PVC's filesystem isn't group-0 writable. Dockerfile chgrps `/data` to GID 0, but the PVC mount must inherit; check StorageClass. |

---

## Safety boundaries

- Token check is HMAC-constant-time (`hmac.compare_digest`).
- Vendor never supplies the Nexus repo or path — only filename.
- Filename must match `^[A-Za-z0-9._-]+$` — no slashes, no spaces, no
  Unicode.
- Path traversal (`..`) rejected at project save time, not at upload time.
- Per-project extension allowlist; blank = any.
- Per-project size cap enforced **during** streaming, not after, so a
  rogue 50 GB upload aborts at the cap and the pod doesn't OOM.
- No vendor-side directory listing endpoint; vendor cannot probe other
  projects.
- Admin URL distribution is out-of-band; vendrop does not email links.

## Known limits

- Single-replica only (file-backed state on one volume). Designed for
  internal vendor handoff workloads, not as a general upload service.
- No vendor login — token-in-URL only. Tokens are bearer credentials;
  rotate them when a vendor engagement ends.
- No virus scanning. The downstream consumer (deploy pipeline) is
  responsible for content trust.
- Audit log isn't shipped off-pod; if the PVC dies, history is lost.
  Backup it if you care about the trail.

## Roadmap

- Optional: per-project upload retention (auto-delete old uploads from
  Nexus after N days).
- Optional: ship audit lines to a log pipeline.

## License

MIT. See `LICENSE`.
