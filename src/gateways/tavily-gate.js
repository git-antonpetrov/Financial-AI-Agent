export default {
  async fetch(request) {
    const url = new URL(request.url);
    url.hostname = 'api.tavily.com';
    return fetch(new Request(url, request));
  },
};
