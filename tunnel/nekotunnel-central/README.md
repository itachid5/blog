# NekoTunnel Central

FastAPI control panel for Railway-backed NekoTunnel service slots.

## Features

- Admin token login using `ADMIN_TOKEN`
- Session cookie auth
- Local SQLite storage at `data/nekotunnel.db`
- Railway account management with masked token display
- Railway Projects page for project-first provisioning
- Service-level Slots page with manual TCP Proxy refresh
- User token generation with one-time full token display
- Provision logs with masked command output
- Placeholder API endpoints for connect, heartbeat, and disconnect
- One-command user installer at `/install.sh`

## Run locally

```bash
cd nekotunnel-central
python -m venv .venv
source .venv/bin/activate
cp .env.example .env
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
```

Open:

```text
http://127.0.0.1:8080/login
```

Log in with the `ADMIN_TOKEN` value from your environment or `.env`.

## Expose with a temporary Pinggy URL

Use the helper script:

```bash
./start_public_pinggy.sh
```

The script starts FastAPI on `127.0.0.1:8080`, starts a Pinggy SSH reverse tunnel, verifies `/health`, and prints the public URL. Stop both processes with:

```bash
./stop_public_pinggy.sh
```

## User installation

Create a user token in the Users page. The full token is shown once; copy it immediately.

One-line install and start command:

```bash
curl -fsSL https://PUBLIC_URL/install.sh | bash -s -- tcp 22 USER_TOKEN https://PUBLIC_URL
```

Manual method:

```bash
curl -fsSL https://PUBLIC_URL/client/nekotunnel -o nekotunnel
chmod +x nekotunnel
./nekotunnel tcp 22 USER_TOKEN https://PUBLIC_URL
```

The installer checks for `bash`/`sh`, `curl` or `wget`, `tar`, and `gzip`. If dependencies are missing, it prints clear install commands for `apt`, `apk`, `yum`, and `pkg`.

The `client/nekotunnel` script automatically downloads `frpc` v0.57.0 if missing. It downloads from this NekoTunnel Central server under `/client/frpc/...`, so the user VM does not need GitHub access and does not need a manual frpc download.

## Railway service workflow

1. Add a Railway account.
2. Create a Railway project from the Projects page.
3. Create services under that project; each service becomes a slot.
4. Enable TCP Proxy manually in Railway for the service with internal port `8080`.
5. Click Refresh TCP on the Slots page.

Automatic TCP Proxy enable is intentionally not implemented.

## Routes

### Admin UI

- `GET /login` — login page
- `POST /login` — admin token login
- `POST /logout` — logout
- `GET /` — dashboard
- `GET /railway-accounts` — Railway accounts page
- `GET /projects` — Railway projects page
- `GET /slots` — service-level slots page
- `GET /users` — users page
- `GET /sessions` — sessions page
- `GET /logs` — audit log page
- `GET /provision-logs` — Railway provisioning logs
- `GET /settings` — settings/status page
- `GET /health` — health check

### User/client endpoints

- `GET /install.sh` — shell installer script
- `GET /client/nekotunnel` — Python client launcher
- `GET /client/frpc/{archive}` — cached frpc v0.57.0 archive served through Central

### Placeholder API

- `POST /api/connect` — returns `{"ok": false, "error": "connect_not_implemented"}`
- `POST /api/heartbeat` — returns `{"ok": true, "message": "heartbeat_placeholder"}`
- `POST /api/disconnect` — returns `{"ok": true, "message": "disconnect_placeholder"}`

## Security notes

- Real Railway tokens are never shown fully in the UI.
- User tokens are shown once and then only a masked prefix is displayed.
- Provision logs mask Railway tokens and FRP tokens.
- Do not commit `.env` or real credentials.
