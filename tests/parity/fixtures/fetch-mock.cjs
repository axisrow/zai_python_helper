// Parity-only fetch interceptor for the upstream @z_ai/coding-helper (issue #17).
//
// The upstream tool's headless auth path validates the API key by making a real
// network call: GET https://api.z.ai/api/coding/paas/v4/models (Bearer), and
// refuses to save the key unless it gets HTTP 200. That makes a Docker parity
// test impossible with a fake key — and we must never send a real key (or hit the
// real endpoint) from CI.
//
// This shim is loaded via NODE_OPTIONS=--require <this file>. It intercepts ONLY
// that single validation call and returns 200 [] so a FAKE key validates offline.
// Every other request passes through to the real globalThis.fetch unchanged, so
// the shim can never mask a bug where the tool hits the wrong URL.
//
// Verified working against @z_ai/coding-helper@0.0.7 (research issue #9).
'use strict';

// Use the GLOBAL Response (exposed by Node >= 18, including the node:22-slim
// base of the parity image). Avoids `require('undici')`, which is not resolvable
// outside a node_modules tree and would break the preload.
const _Response = globalThis.Response;
const _origFetch = globalThis.fetch.bind(globalThis);

globalThis.fetch = async function parityFetch(input, init) {
  try {
    const url =
      typeof input === 'string'
        ? new URL(input)
        : input && typeof input.url === 'string'
          ? new URL(input.url)
          : null;
    const method = (
      (init && init.method) ||
      (input && input.method) ||
      'GET'
    ).toUpperCase();
    if (
      url &&
      url.hostname === 'api.z.ai' &&
      url.pathname === '/api/coding/paas/v4/models' &&
      method === 'GET'
    ) {
      return new _Response('[]', {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }
  } catch (_) {
    // Parse error: fall through to the real fetch so failures stay realistic.
  }
  return _origFetch(input, init);
};
