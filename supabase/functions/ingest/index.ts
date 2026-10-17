// ==============================================================================
// Tether: Supabase Ingest Edge Function (Deno / TypeScript)
// Validates webhook signature, normalizes payloads, and atomically enqueues runs.
// ==============================================================================

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.0";

// Supabase environment variables provided automatically in Edge Functions
const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const WEBHOOK_SECRET = Deno.env.get("WEBHOOK_SECRET") ?? "";

// Pre-initialize Supabase service role client
const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, {
  auth: {
    persistSession: false,
    autoRefreshToken: false,
  },
});

/**
 * Verify HMAC-SHA256 signature using Web Crypto API.
 */
async function verifyHmacSignature(
  rawBody: string,
  providedSignature: string | null,
  secret: string
): Promise<boolean> {
  if (!providedSignature || !secret) {
    return false;
  }

  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"]
  );

  // Convert hex string to Uint8Array
  const sigBytes = new Uint8Array(
    providedSignature.match(/.{1,2}/g)?.map((byte) => parseInt(byte, 16)) ?? []
  );

  return await crypto.subtle.verify(
    "HMAC",
    key,
    sigBytes,
    encoder.encode(rawBody)
  );
}

/**
 * Map inbound webhook topic / source to target agent ID.
 */
function resolveAgentId(body: Record<string, unknown>, url: URL): string {
  // 1. Explicit path parameter or query parameter: /ingest?agent_id=xyz
  const queryAgent = url.searchParams.get("agent_id");
  if (queryAgent) return queryAgent;

  // 2. Body level routing
  if (typeof body.agent_id === "string") return body.agent_id;

  // 3. Event-type heuristic routing
  const eventType = String(body.type || body.event || "");
  if (eventType.startsWith("incident.") || eventType.startsWith("alert.")) {
    return "triage_agent";
  }
  if (eventType.startsWith("digest.") || eventType.startsWith("report.")) {
    return "demo_digest";
  }

  // Default fallback agent
  return "demo_digest";
}

serve(async (req: Request) => {
  // CORS Preflight
  if (req.method === "OPTIONS") {
    return new Response("ok", {
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "*",
      },
    });
  }

  if (req.method !== "POST") {
    return new Response(JSON.stringify({ error: "Method not allowed. Use POST." }), {
      status: 405,
      headers: { "Content-Type": "application/json" },
    });
  }

  try {
    const rawBody = await req.text();
    let body: Record<string, unknown> = {};

    try {
      body = JSON.parse(rawBody);
    } catch {
      return new Response(JSON.stringify({ error: "Invalid JSON request body" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    // 1. Validate HMAC signature
    const signatureHeader = req.headers.get("X-Webhook-Signature");
    if (!WEBHOOK_SECRET) {
      console.error("CRITICAL: WEBHOOK_SECRET is not configured in environment. Rejecting webhook for security.");
      return new Response(
        JSON.stringify({ error: "Server configuration error: WEBHOOK_SECRET is required" }),
        { status: 500, headers: { "Content-Type": "application/json" } }
      );
    }
    const isValid = await verifyHmacSignature(rawBody, signatureHeader, WEBHOOK_SECRET);
    if (!isValid) {
      return new Response(
        JSON.stringify({ error: "Unauthorized: Invalid X-Webhook-Signature" }),
        {
          status: 401,
          headers: { "Content-Type": "application/json" },
        }
      );
    }

    // 2. Resolve Agent and Deduplication Key
    const url = new URL(req.url);
    const agentId = resolveAgentId(body, url);

    const eventId =
      req.headers.get("X-Event-ID") ||
      String(body.id || body.event_id || "");

    const dedupKey = eventId ? `${agentId}:${eventId}` : null;

    // 3. Atomically enqueue via PostgreSQL RPC function
    const { data, error } = await supabase.rpc("tether_enqueue", {
      p_agent_id: agentId,
      p_trigger: "webhook",
      p_payload: body,
      p_dedup_key: dedupKey,
    });

    if (error) {
      console.error("tether_enqueue RPC error:", error);
      return new Response(JSON.stringify({ error: error.message }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      });
    }

    const result = data && data[0] ? data[0] : null;

    return new Response(
      JSON.stringify({
        run_id: result?.run_id,
        is_duplicate: result?.is_duplicate ?? false,
        state: result?.state ?? "queued",
        agent_id: agentId,
        message: result?.is_duplicate
          ? "Duplicate event detected. Run already enqueued or processed."
          : "Agent run enqueued successfully.",
      }),
      {
        status: result?.is_duplicate ? 200 : 201,
        headers: {
          "Content-Type": "application/json",
          "Access-Control-Allow-Origin": "*",
        },
      }
    );
  } catch (err: unknown) {
    const errorMsg = err instanceof Error ? err.message : String(err);
    console.error("Unhandled Edge Function error:", errorMsg);
    return new Response(JSON.stringify({ error: errorMsg }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    });
  }
});
