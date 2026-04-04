# UniFi Gate Auth Worker

Cloudflare Worker that validates Firebase JWTs at the edge before requests reach your server.

## Setup

1. **Copy the config template:**
   ```bash
   cp wrangler.toml.example wrangler.toml
   ```

2. **Create a KV namespace:**
   ```bash
   npx wrangler kv:namespace create "APPROVED_USERS"
   ```
   Copy the returned namespace ID into `wrangler.toml`.

3. **Update routes** in `wrangler.toml` with your domain.

4. **Set secrets:**
   ```bash
   npx wrangler secret put FIREBASE_PROJECT_ID
   npx wrangler secret put ORIGIN_URL
   ```

5. **Deploy:**
   ```bash
   npx wrangler deploy
   ```

## How It Works

The worker intercepts all requests to your domain routes, validates the Firebase JWT in the Authorization header, checks the user's email against the KV approved users list, and forwards valid requests to your origin server via the Cloudflare tunnel.
