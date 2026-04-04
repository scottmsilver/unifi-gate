# UniFi Access Control System

## Project Overview
Flask-based web server for controlling UniFi Access door locks, with a TUI, Android app, and optional Cloudflare-tunneled remote access with Firebase authentication.

## Key Files
- `server.py` -- Main Flask server
- `unifi_native_api.py` -- Reverse-engineered API for hold-open functionality
- `unifi_access_api.py` -- Official UniFi Developer API client
- `unifi_websocket.py` -- Real-time WebSocket event handler
- `schedule_manager.py` -- Hold-open schedule injection
- `hold_state_manager.py` -- Persistent hold state tracking
- `schedule_journal.py` -- Schedule change logging
- `user_store.py` -- User management (SQLite/JSON)
- `kv_sync.py` -- Cloudflare KV synchronization
- `manage_users.py` -- CLI user management tool
- `worker/src/index.js` -- Cloudflare Worker (edge auth)
- `worker/wrangler.toml.example` -- Worker config template
- `scripts/deploy.sh` -- Deploy to Incus container

## Running Locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Edit with your config
python server.py --dev  # Dev mode disables auth
```

## Deploying

```bash
./scripts/deploy.sh [container-name]  # Deploys to Incus container
./scripts/deploy.sh mycontainer dev   # Dev mode (auth disabled)
```

## Common Operations

```bash
# User management
python manage_users.py list
python manage_users.py set-admin email@example.com

# Worker deployment
cd worker && cp wrangler.toml.example wrangler.toml  # Fill in your values
npx wrangler deploy
```

## Notes
- Never hardcode server URLs in code
- `--dev` flag or `DEV_MODE=true` disables all authentication
- Production state files are NOT overwritten by deploys
