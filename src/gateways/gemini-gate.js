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

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "POST, OPTIONS",
          "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Gateway-Secret",
        },
      });
    }

    // 1. Проверка авторизации шлюза (Zero-Trust Fail-Closed Authentication)
    const expectedSecret = env.GATEWAY_SECRET || env.GEMINI_GATEWAY_SECRET;
    if (!expectedSecret) {
      return new Response(
        JSON.stringify({ error: "Server Configuration Error: GATEWAY_SECRET is not configured (Fail-Closed Zero-Trust)" }),
        { status: 500, headers: { "Content-Type": "application/json" } }
      );
    }

    const clientSecret = request.headers.get("X-Gateway-Secret") ||
      (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
    if (!clientSecret || !timingSafeEqual(clientSecret, expectedSecret)) {
      return new Response(JSON.stringify({ error: "Unauthorized: Invalid or missing gateway secret" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      });
    }

    // 2. Только разрешенные методы
    if (request.method !== "POST") {
      return new Response(JSON.stringify({ error: "Method not allowed" }), {
        status: 405,
        headers: { "Content-Type": "application/json" },
      });
    }

    // 3. Безопасное проксирование на целевой хост Google Cloud
    const url = new URL(request.url);
    url.hostname = "aiplatform.googleapis.com";
    url.protocol = "https:";

    // Очистка внутреннего секрета перед отправкой запроса внешнему провайдеру (Zero-Trust Data Protection)
    const forwardHeaders = new Headers(request.headers);
    forwardHeaders.delete("x-gateway-secret");
    forwardHeaders.delete("X-Gateway-Secret");

    const forwardRequest = new Request(url, {
      method: request.method,
      headers: forwardHeaders,
      body: request.body,
      redirect: request.redirect,
    });
    return fetch(forwardRequest);
  },
};
