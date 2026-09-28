export default {
  async fetch(request) {
    const url = new URL(request.url);
    url.hostname = "aiplatform.googleapis.com";
    return fetch(new Request(url, request));
  }
};
