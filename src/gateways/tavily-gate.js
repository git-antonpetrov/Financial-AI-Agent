/**
 * Cloudflare Worker Proxy for Tavily (tavily-gate)
 *
 * Вставь сюда свой код для Cloudflare Worker, который пересылает запросы к Tavily.
 */
export default {
  async fetch(request, env, ctx) {
    // Твой код воркера
    return new Response("Tavily Gateway Node", { status: 200 });
  },
};
