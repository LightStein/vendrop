# Vendrop — Status

Full documentation: [`README.md`](README.md)

## Current state — 2026-05-13

- **Version:** v1.0.1
- **DEV namespace:** `dev-devops-tools`
- **Route:** `https://vendrop.apps.devocp.anb.net`
- **Image:** `docker.io/nautilus444/vendrop:v1.0.1` (bootstrap; Nexus mirror pending)
- **GitOps:** chart at `workspace/deployments-main/projects/vendrop/`, ArgoCD app at `envs/dev/templates/dev-vendrop.yaml`
- **Secret:** `vendrop-secrets` in `dev-devops-tools` (out-of-band, NEXUS_*, ADMIN_*, SECRET_KEY)

## Changelog

### v1.0.1 — 2026-05-13
- Fix: wired up Werkzeug `ProxyFix` so `request.host_url` honors
  `X-Forwarded-Proto` from the OCP Route. Generated vendor URLs are now
  `https://` directly — no more http→https redirect cancellation in the
  browser.

### v1.0.0 — 2026-05-12
- First deploy. Admin UI + vendor drop page + streaming PUT to Nexus
  raw + JSONL audit on PVC.

## Open items

- Mirror image to `registry.nexus.anb.net/devops/vendrop` and flip the
  dev chart back to the Nexus path.
- Stand up UAT app once DEV has a vendor flow exercised end-to-end.
