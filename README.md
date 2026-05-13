# Vendrop

Browser-based upload tool that lets vendors drop archives into ANB Nexus
without giving them Nexus credentials, console access, or visibility into
anyone else's path. Admin (DevOps) defines the project and Nexus path;
vendor gets a tokenised URL and a drag-and-drop page.

The problem this solves: vendors hand-deliver builds (WAR, ZIP, tarballs)
and we have nowhere good to put them. Email-sized attachments don't fit,
S3 / SFTP needs new accounts per vendor, and giving them Nexus logins
pollutes the registry. Vendrop is a thin gate in front of the Nexus raw
API: admin-curated paths in, vendor uploads out, full audit trail.

---

## Architecture

```
   vendor browser                 vendrop pod (dev-devops-tools)         Nexus raw
   ┌────────────┐   HTTPS    ┌───────────────────────────────┐  HTTPS  ┌──────────┐
   │ /u/<pid>?t │─────────▶  │ Flask + Werkzeug ProxyFix      │ ──────▶ │  /repo/  │
   │            │            │  • token check                  │   PUT   │  <path>/ │
   │ drag/drop  │            │  • extension + size check       │         │  <file>  │
   └────────────┘            │  • stream PUT to Nexus          │         └──────────┘
                             │  • append audit.jsonl           │
                             │                                 │
                             │  /data  ──▶ PVC (RWO, 1Gi)      │
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
| `deployment.yaml`                 | Standalone manifest (reference / non-GitOps)         |
| `STATUS.md`                       | Current deployment state                             |
| `chart/`                          | Helm chart (canonical deploy)                        |
| `chart/values.yaml`               | Per-env image / config / resources                   |

---

## Configuration

All config is env-based. Non-secret values come from the chart's `config`
block (rendered into a `ConfigMap`); secrets come from a `vendrop-secrets`
`Secret` created out-of-band.

| Variable           | Source     | Default                                                          | Notes |
| ------------------ | ---------- | ---------------------------------------------------------------- | ----- |
| `NEXUS_RAW_BASE`   | ConfigMap  | `https://nxrm-operator-certified-nexus.apps.ocpdev.anb.net`      | Trailing slash stripped |
| `NEXUS_VERIFY_TLS` | ConfigMap  | `true`                                                           | `false` in DEV (self-signed) |
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

## Deploy (canonical, GitOps)

Helm chart is at `chart/`. Wire it to your namespace via the GitOps tool
of your choice (e.g. ArgoCD `Application` pointing `path: chart` at this
repo). ANB runs it under `dev-devops-tools`.

```bash
# 1. Create the namespace (one-off, requires cluster-admin)
oc apply -f outgoing/dev-devops-tools.yml

# 2. Create the secret (one-off, out-of-band; never in Git)
oc create secret generic vendrop-secrets -n dev-devops-tools \
  --from-literal=NEXUS_USER=admin \
  --from-literal=NEXUS_PASS='<nexus password>' \
  --from-literal=ADMIN_USER=admin \
  --from-literal=ADMIN_PASS='<choose one>' \
  --from-literal=SECRET_KEY="$(openssl rand -hex 32)"

# 3. Push the chart + ArgoCD Application via the GitOps zip workflow
#    (gateway ships outgoing/vendrop-gitops.zip + COMMIT_MESSAGES.txt)
#    Once merged, ArgoCD syncs.

# 4. Find the URL
oc get route vendrop -n dev-devops-tools
# → https://vendrop.apps.devocp.anb.net
```

The chart's Route ships `tls.termination: edge` + `insecureEdgeTerminationPolicy: Redirect`.
The Flask app has `ProxyFix` wired up so `request.host_url` returns `https://` —
that means generated vendor links land on HTTPS directly and don't trip
the redirect mid-flight.

### Rolling a new image

```bash
# Local build (Docker Hub during bootstrap; Nexus once mirrored)
docker build -t nautilus444/vendrop:vX.Y.Z .
docker push  nautilus444/vendrop:vX.Y.Z

# Bump image.tag in envs/<env>/templates/<env>-vendrop.yaml
# Push via the GitOps zip workflow → ArgoCD rolls the Deployment
```

---

## Admin guide

1. Open the Route (`https://vendrop.apps.devocp.anb.net`).
2. Sign in with `ADMIN_USER` / `ADMIN_PASS` (from the `vendrop-secrets`).
3. **Create project** — `/admin/new`. Fields:
   - **Name**: display only (e.g. "EPS").
   - **Nexus repo**: existing raw repo name (e.g. `raw`).
   - **Path prefix**: where uploads land inside the repo (e.g. `eps/`).
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

Vendor receives a URL like `https://vendrop.apps.devocp.anb.net/u/abc123?t=...`.
They open it, drag a file onto the page, wait for the green tick.
Failures show the reason inline (wrong extension, too big, Nexus 4xx/5xx).
That is the entire vendor-facing surface.

---

## Operations

### Audit log

Append-only JSONL at `/data/audit.jsonl`. One line per successful upload:

```json
{"ts":"2026-05-13T09:42:11Z","project":"abc123","name":"eps.war",
 "bytes":12345678,"sha256":"...","dest":"https://.../raw/eps/eps.war",
 "remote":"10.x.x.x","ua":"Mozilla/...","duration_ms":842}
```

To grep:

```bash
oc rsh -n dev-devops-tools deploy/vendrop \
  cat /data/audit.jsonl | jq 'select(.project=="abc123")'
```

### Backup

Two files on the PVC need a backup if you don't want to recreate state by
hand:

- `/data/projects.json` — project definitions + tokens.
- `/data/audit.jsonl`   — upload history.

A nightly `oc cp` to a sidecar or external bucket is enough; both files
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

- Single-replica only (file-backed state on RWO PVC). Designed for
  internal vendor handoff workloads, not as a general upload service.
- No vendor login — token-in-URL only. Tokens are bearer credentials;
  rotate them when a vendor engagement ends.
- No virus scanning. The downstream consumer (deploy pipeline) is
  responsible for content trust.
- Audit log isn't shipped off-pod; if the PVC dies, history is lost.
  Backup it if you care about the trail.

## Roadmap

- Mirror the image to `registry.nexus.anb.net/devops/vendrop` so we can
  drop the `docker.io/nautilus444` bootstrap path.
- Optional: per-project upload retention (auto-delete old uploads from
  Nexus after N days).
- Optional: hook audit lines into Splunk / Dynatrace.
