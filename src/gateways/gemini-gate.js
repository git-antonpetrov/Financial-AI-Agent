/**
 * Защищенный шлюз Cloudflare Worker для обращения к Google Gemini / Vertex AI.
 * Реализует строгую аутентификацию входящих запросов по секретному токену GATEWAY_SECRET.
 */
function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  const aBuf = new TextEncoder().encode(a);
  const bBuf = new TextEncoder().encode(b);
  if (aBuf.byteLength !== bBuf.byteLength) return false;
  let diff = 0;
  for (let i = 0; i < aBuf.byteLength; i++) {
    diff |= aBuf[i] ^ bBuf[i];
  }
  return diff === 0;
}

const DEFAULT_ALLOWED_ORIGINS = [
  "https://admin.fin-ai-agent.ru",
  "http://localhost:5173",
  "http://localhost:8001",
  "http://localhost:8002",
  "tauri://localhost",
  "https://tauri.localhost",
  "http://tauri.localhost",
  "asset://localhost",
];

function getAllowedOrigins(env) {
  const configured = env.CORS_ALLOWED_ORIGINS || env.ALLOWED_ORIGINS;
  if (typeof configured === "string" && configured.trim().length > 0) {
    return configured
      .split(",")
      .map((o) => o.trim())
      .filter((o) => o && o !== "*");
  }
  return DEFAULT_ALLOWED_ORIGINS;
}

function getCorsOrigin(request, env) {
  const origin = request.headers.get("Origin");
  if (!origin) return null;
  const allowed = getAllowedOrigins(env);
  return allowed.includes(origin) ? origin : false;
}

function applyCorsHeaders(response, allowedOrigin) {
  if (!allowedOrigin) return response;
  const headers = new Headers(response.headers);
  headers.set("Access-Control-Allow-Origin", allowedOrigin);
  headers.set("Access-Control-Allow-Credentials", "true");
  headers.set("Vary", "Origin");
  const body = [204, 205, 304].includes(response.status) ? null : response.body;
  return new Response(body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

export default {
  async fetch(request, env = {}) {
    const origin = request.headers.get("Origin");
    const allowedOrigin = getCorsOrigin(request, env);

    // 0. Валидация CORS: отклонение неразрешенных источников (предотвращение атак через поддельные Origin)
    if (origin && !allowedOrigin) {
      return new Response(
        JSON.stringify({ error: "Forbidden: Origin not allowed by CORS policy" }),
        {
          status: 403,
          headers: { "Content-Type": "application/json" },
        }
      );
    }

    if (request.method === "OPTIONS") {
      if (!origin) {
        return new Response(
          JSON.stringify({ error: "Forbidden: Missing Origin header for CORS preflight" }),
          {
            status: 403,
            headers: { "Content-Type": "application/json" },
          }
        );
      }
      return new Response(null, {
        status: 204,
        headers: {
          "Access-Control-Allow-Origin": allowedOrigin,
          "Access-Control-Allow-Methods": "POST, OPTIONS",
          "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Gateway-Secret",
          "Access-Control-Allow-Credentials": "true",
          "Access-Control-Max-Age": "86400",
          "Vary": "Origin",
        },
      });
    }

    // 1. Проверка авторизации шлюза (Zero-Trust Fail-Closed Authentication)
    const expectedSecret = env.GATEWAY_SECRET || env.GEMINI_GATEWAY_SECRET;
    if (!expectedSecret) {
      return applyCorsHeaders(
        new Response(
          JSON.stringify({ error: "Server Configuration Error: GATEWAY_SECRET is not configured (Fail-Closed Zero-Trust)" }),
          { status: 500, headers: { "Content-Type": "application/json" } }
        ),
        allowedOrigin
      );
    }

    const clientSecret = request.headers.get("X-Gateway-Secret") ||
      (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
    if (!clientSecret || !timingSafeEqual(clientSecret, expectedSecret)) {
      return applyCorsHeaders(
        new Response(JSON.stringify({ error: "Unauthorized: Invalid or missing gateway secret" }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        }),
        allowedOrigin
      );
    }

    // 2. Только разрешенные методы
    if (request.method !== "POST") {
      return applyCorsHeaders(
        new Response(JSON.stringify({ error: "Method not allowed" }), {
          status: 405,
          headers: { "Content-Type": "application/json" },
        }),
        allowedOrigin
      );
    }

    // 3. Безопасное проксирование на целевой хост Google Cloud
    const url = new URL(request.url);
    url.hostname = "aiplatform.googleapis.com";
    url.protocol = "https:";
    url.port = "";

    // Очистка внутреннего секрета перед отправкой запроса внешнему провайдеру (Zero-Trust Data Protection)
    const forwardHeaders = new Headers(request.headers);
    forwardHeaders.delete("x-gateway-secret");
    forwardHeaders.delete("X-Gateway-Secret");

    const authHeader = forwardHeaders.get("authorization") || forwardHeaders.get("Authorization");
    if (authHeader && timingSafeEqual(authHeader.replace(/^Bearer\s+/i, ""), expectedSecret)) {
      forwardHeaders.delete("authorization");
      forwardHeaders.delete("Authorization");
    }

    // Защита от SSRF и открытых перенаправлений: принудительный режим redirect: 'manual'
    const requestInit = {
      method: request.method,
      headers: forwardHeaders,
      body: request.body,
      redirect: "manual",
    };
    if (request.body) {
      requestInit.duplex = "half";
    }

    const forwardRequest = new Request(url, requestInit);
    try {
      // Строгий таймаут апстрима 10с (AbortSignal.timeout(10000))
      const response = await fetch(forwardRequest, {
        signal: AbortSignal.timeout(10000),
        redirect: "manual",
      });
      return applyCorsHeaders(response, allowedOrigin);
    } catch (err) {
      const isTimeout =
        err?.name === "TimeoutError" ||
        err?.name === "AbortError" ||
        err?.cause?.name === "TimeoutError" ||
        err?.cause?.name === "AbortError";
      return applyCorsHeaders(
        new Response(
          JSON.stringify({
            error: isTimeout
              ? "Upstream gateway request timed out"
              : "Upstream gateway connection failed",
          }),
          {
            status: isTimeout ? 504 : 502,
            headers: { "Content-Type": "application/json" },
          }
        ),
        allowedOrigin
      );
    }
  },
};
