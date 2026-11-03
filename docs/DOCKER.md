# Docker

Image sizes, the reasoning behind the base image, and the hardening in the compose file.
The one-line version is in the [README](../README.md#quick-start).

Best if you want the web service to stay running somewhere without managing a virtualenv.

```bash
docker build -t aion2-api .
docker run -d --name aion2 -p 8000:8000 aion2-api
curl "http://127.0.0.1:8000/openapi.json"
```

The image is a two-stage build: one stage resolves `uv.lock` into a virtualenv, the other copies
that venv plus `app.py` and `aion2_api.py` onto a clean base. The `uv` binary, the package caches
and every build tool stay behind in the first stage, and the dev group (`httpx2`, `httpx`) is never
installed — so the image carries the runtime and nothing else. It runs as **uid 10001**, not root,
and ships a `HEALTHCHECK` that polls `/` with the stdlib, so `docker ps` shows health without any
extra tooling in the image.

**Measured size**, same application and same lock, built on amd64:

| Base | Final image | Verdict |
|---|---|---|
| `python:3.13-alpine` | **91 MB** | used |
| `python:3.13-slim-bookworm` | 168 MB | works; ~77 MB heavier |
| `gcr.io/distroless/python3-debian12` | — | rejected: that image ships Python 3.11, so it would silently drop the project off 3.13 |

Both the Alpine and Debian builds were run end to end, including real upstream calls
(`/notices`, `/events`), not just a static `/` probe — musl's resolver and CA bundle are exercised,
not assumed. Alpine is chosen for size; if a future dependency ships no musllinux wheel, switch both
`FROM` lines to `python:3.13-slim-bookworm`, drop the `apk` lines, and use `groupadd`/`useradd`
instead of `addgroup`/`adduser`. The `Dockerfile` says so at the top.

CI builds the image on every push and pull request and checks that it starts, serves `/openapi.json`
and runs as a non-root user, so the Dockerfile cannot rot unnoticed.

To run it as a managed service instead of a bare `docker run`, use the bundled compose file:

```bash
docker compose up -d --build     # build the image and start it
docker compose ps                # health comes from the image's own HEALTHCHECK
docker compose logs -f api
docker compose down
```

`compose.yaml` builds from the same `Dockerfile` and adds what a `docker run` line leaves you to
remember: `restart: unless-stopped`, a configurable host port (`AION2_PORT=9000 docker compose up -d`),
and real hardening — **read-only root filesystem**, a writable `/tmp` tmpfs, `no-new-privileges`
and a small init as PID 1. Those were tested against this image rather than assumed: `touch /`
fails with `Read-only file system` while `touch /tmp/canary` succeeds, and the service still answers
`/openapi.json`.

## Locking it to your backend

Deployed publicly, anyone who finds the URL can call it, and every caller shares the same polite
upstream budget (one request per second per host) and the same outbound IP. When only your own
server should call it, for example a Next.js backend on Vercel, set a shared secret:

| Variable | Where | Effect |
|---|---|---|
| `AION2_API_INTERNAL_KEY` | The API's environment (e.g. Railway) | Every route except `/` answers `401 {"detail": "Missing or invalid internal key"}` unless the request sends the same value in the `X-Internal-Key` header. |

- **Unset or blank:** the API behaves exactly as before, open to everyone. Local development,
  `docker run` and CI need nothing.
- **`/` stays open.** It is static, never calls upstream, and is what a platform health check
  should probe. `/docs` and `/openapi.json` are behind the key like everything else.
- The comparison is constant-time (`hmac.compare_digest`). The key is never echoed in a response
  and never logged.

Generate a long random value, and paste it straight into each platform's secret settings. Never
commit it, and never put a real value in docs or issues:

```bash
openssl rand -hex 32
# or, without openssl:
python -c "import secrets; print(secrets.token_hex(32))"
```

The **same value** goes in two places:

1. **The API** (Railway → service → Variables): `AION2_API_INTERNAL_KEY`.
2. **The calling server** (Vercel → project → Environment Variables): a server-only variable,
   e.g. `AION2_API_INTERNAL_KEY`. Never a `NEXT_PUBLIC_` name, because those are bundled into
   browser JavaScript.

On the Next.js side, call the API only from server code (route handlers, server components, server
actions) and send the header there:

```ts
import "server-only";

export async function aion2(path: string) {
  return fetch(`${process.env.AION2_API_URL}${path}`, {
    headers: { "X-Internal-Key": process.env.AION2_API_INTERNAL_KEY! },
  });
}
```

To rotate the key, set a new value on both sides and redeploy both. Requests are refused with `401`
until the two match again.

Railway runs this image as it is. Set `PORT=8000` so Railway routes to the port the image listens
on, point the service's health check at `/`, and keep a single replica. The cache and the
per-host throttle live in process memory, so more replicas would multiply the request rate to
upstream.

---
