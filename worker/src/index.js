/**
 * UniFi Gate Auth Worker
 *
 * Validates Firebase JWT tokens at the edge and checks user approval in KV.
 * Only approved users are forwarded to the origin (Cloudflare Tunnel).
 *
 * Environment variables (set via wrangler secret):
 *   FIREBASE_PROJECT_ID - Your Firebase project ID
 *   ORIGIN_URL - Your Cloudflare Tunnel origin URL
 *
 * KV Bindings:
 *   APPROVED_USERS - KV namespace with approved user emails as keys
 *   POOL_APPROVED_USERS - KV namespace with pool-specific approved users
 */

import { createRemoteJWKSet, jwtVerify } from "jose";

// Firebase JWKS endpoint for token verification
const FIREBASE_JWKS = createRemoteJWKSet(
  new URL("https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com")
);

/**
 * Verify a Firebase JWT token using jose library.
 * Returns the decoded payload if valid, null otherwise.
 */
async function verifyFirebaseToken(token, projectId) {
  try {
    const expectedIssuer = `https://securetoken.google.com/${projectId}`;

    const { payload } = await jwtVerify(token, FIREBASE_JWKS, {
      issuer: expectedIssuer,
      audience: projectId,
      algorithms: ["RS256"],
      clockTolerance: 5, // 5 seconds clock skew
    });

    if (!payload.email) {
      return { valid: false, error: "No email in token" };
    }

    if (!payload.email_verified) {
      return { valid: false, error: "Email not verified" };
    }

    return { valid: true, payload };
  } catch (e) {
    return { valid: false, error: e.message };
  }
}

/**
 * Check if a user email is approved in KV.
 */
async function isUserApproved(email, kv) {
  const value = await kv.get(email);
  return value !== null;
}

/**
 * Main request handler.
 */
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Allow OPTIONS requests for CORS preflight
    if (request.method === "OPTIONS") {
      return handleCors(request);
    }

    // Public endpoints that don't require auth
    // Pool UI: only / is public (serves the login-gated HTML). All /api/* require auth.
    // Gate UI: /, /admin, /static/, /invite/, /door-image/ are public.
    const isPool = url.hostname.startsWith('pool.');

    // Handle LAN-based approval callback (self-contained, no auth header needed)
    if (isPool && url.pathname === '/api/approve-callback') {
      return handleApproveCallback(url, env, request);
    }

    const publicPaths = isPool ? [
      "/",           // Pool login page (auth handled client-side)
      "/matter",     // Matter pairing page (QR code + manual code)
      "/favicon.ico",
      "/robots.txt",
    ] : [
      "/",           // Root page (login UI)
      "/admin",      // Admin page (frontend handles auth)
      "/invite/",    // Invite acceptance page
      "/favicon.ico",
      "/robots.txt",
      "/static/",    // Static assets
      "/door-image/", // Door snapshots (loaded by img tags without auth header)
    ];

    // Allow root page and static assets without auth (so users can see login UI)
    const isPublic = publicPaths.some(path => {
      if (path === "/" || path === "/admin") {
        // Exact match for root and admin page
        return url.pathname === path;
      }
      // Prefix match for paths ending with /
      return url.pathname.startsWith(path);
    });

    if (isPublic) {
      return forwardToOrigin(request, env, null);
    }

    // Check for Authorization header (or token query param for WebSocket upgrades only)
    let authHeader = request.headers.get("Authorization");
    if (!authHeader && url.searchParams.has("token") && request.headers.get("Upgrade") === "websocket") {
      authHeader = "Bearer " + url.searchParams.get("token");
    }

    if (!authHeader || !authHeader.startsWith("Bearer ")) {
      return new Response(JSON.stringify({ error: "Missing or invalid Authorization header" }), {
        status: 401,
        headers: {
          "Content-Type": "application/json",
          ...getCorsHeaders(request),
        },
      });
    }

    const token = authHeader.substring(7);

    // Verify the Firebase token
    const projectId = env.FIREBASE_PROJECT_ID;
    if (!projectId) {
      console.error("FIREBASE_PROJECT_ID not configured");
      return new Response(JSON.stringify({ error: "Server misconfigured" }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      });
    }

    const result = await verifyFirebaseToken(token, projectId);

    if (!result.valid) {
      return new Response(JSON.stringify({ error: result.error }), {
        status: 401,
        headers: {
          "Content-Type": "application/json",
          ...getCorsHeaders(request),
        },
      });
    }

    const email = result.payload.email;

    // Select KV namespace based on hostname (pool has its own user list)
    const hostname = url.hostname;
    let approvedUsersKv;
    if (hostname.startsWith('pool.')) {
      if (!env.POOL_APPROVED_USERS) {
        return new Response(JSON.stringify({ error: "Pool approval list not configured" }), {
          status: 503, headers: { "Content-Type": "application/json" },
        });
      }
      approvedUsersKv = env.POOL_APPROVED_USERS;
    } else {
      approvedUsersKv = env.APPROVED_USERS;
    }

    // Check if user is approved in KV
    const approved = await isUserApproved(email, approvedUsersKv);

    if (!approved) {
      return new Response(JSON.stringify({ error: "User not approved", email }), {
        status: 403,
        headers: {
          "Content-Type": "application/json",
          ...getCorsHeaders(request),
        },
      });
    }

    // Forward to origin with verified user header
    return forwardToOrigin(request, env, email);
  },
};

/**
 * Forward request to the origin (Cloudflare Tunnel).
 *
 * When the Worker is on a route that has a Cloudflare Tunnel behind it,
 * we pass the request through to the tunnel origin by fetching the
 * original URL. Cloudflare routes the fetch to the tunnel, not back
 * to the Worker.
 *
 * If ORIGIN_URL is set, the request is rewritten to that URL instead
 * (for setups where the Worker and tunnel are on different hostnames).
 */
async function forwardToOrigin(request, env, verifiedEmail) {
  // Clone headers and add verified user
  const headers = new Headers(request.headers);

  if (verifiedEmail) {
    headers.set("X-Verified-User", verifiedEmail);
  }

  // Remove the Authorization header (origin doesn't need it)
  headers.delete("Authorization");

  // Determine the target URL
  let targetUrl;
  if (env.ORIGIN_URL) {
    const url = new URL(request.url);
    targetUrl = new URL(url.pathname + url.search, env.ORIGIN_URL).toString();
  } else {
    targetUrl = request.url;
  }

  // Forward the request to the origin
  const response = await fetch(targetUrl, {
    method: request.method,
    headers,
    body: request.body,
  });

  // Clone response and add CORS headers
  const newResponse = new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });

  // Add CORS headers to response
  const corsHeaders = getCorsHeaders(request);
  for (const [key, value] of Object.entries(corsHeaders)) {
    newResponse.headers.set(key, value);
  }

  return newResponse;
}

/**
 * Handle CORS preflight requests.
 */
function handleCors(request) {
  return new Response(null, {
    status: 204,
    headers: getCorsHeaders(request),
  });
}

/**
 * Get allowed origin for CORS.
 * Allows the requesting origin if it matches the Worker route's hostname,
 * plus localhost for development.
 */
function getAllowedOrigin(request) {
  const origin = request.headers.get("Origin");
  if (!origin) return null;

  // Always allow localhost for development
  if (origin.startsWith("http://localhost:")) return origin;

  // Allow any origin that matches the request's own hostname (Worker routes control access)
  try {
    const requestHost = new URL(request.url).hostname;
    const originHost = new URL(origin).hostname;
    if (requestHost === originHost) return origin;
  } catch (e) {
    // Invalid URL, deny
  }

  return null;
}

function getCorsHeaders(request) {
  const origin = getAllowedOrigin(request);
  return {
    "Access-Control-Allow-Origin": origin || "",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Allow-Credentials": "true",
  };
}

/**
 * Handle LAN-based approval callback.
 * The daemon on the local network signs an HMAC proof that the user
 * was physically on the home WiFi. We validate it and add them to KV.
 */
async function handleApproveCallback(url, env, request) {
  const email = url.searchParams.get("email");
  const ts = url.searchParams.get("ts");
  const sig = url.searchParams.get("sig");

  if (!email || !ts || !sig) {
    return new Response("Missing parameters", { status: 400 });
  }

  // Reject proofs older than 5 minutes
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - parseInt(ts, 10)) > 300) {
    return new Response("Approval link expired. Go back and try again.", {
      status: 400,
      headers: { "Content-Type": "text/plain" },
    });
  }

  const secret = env.NETWORK_SECRET;
  if (!secret) {
    return new Response("Server not configured for LAN approval", { status: 503 });
  }

  // Verify HMAC-SHA256 signature
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const expected = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(`${email}|${ts}`),
  );
  const expectedHex = Array.from(new Uint8Array(expected))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");

  if (expectedHex !== sig) {
    return new Response("Invalid approval signature", { status: 403 });
  }

  // Add user to approved list
  if (!env.POOL_APPROVED_USERS) {
    return new Response("Pool approval list not configured", { status: 503 });
  }
  await env.POOL_APPROVED_USERS.put(email, "approved-via-lan");

  // Redirect to pool UI
  return new Response(null, {
    status: 302,
    headers: { Location: "/" },
  });
}
