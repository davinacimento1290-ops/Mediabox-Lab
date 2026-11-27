# MEDIABOX Cloudflare Worker gateway

This Worker forwards requests only to the existing MEDIABOX Flask service. It does not replace, copy, or rewrite the Python backend.

## Deploy

1. Sign in to the Cloudflare dashboard.
2. Open **Workers & Pages** and create a Worker.
3. Name it `mediabox-gateway` (or another available name).
4. Open **Edit code**, replace the starter code with the complete contents of `cloudflare/worker.js`, and deploy.
5. In the Worker settings, enable the `workers.dev` route/subdomain if Cloudflare asks.
6. Copy the full HTTPS URL Cloudflare assigns, for example `https://mediabox-gateway.<account-subdomain>.workers.dev`.

## Finish Android configuration

Send the exact assigned `workers.dev` URL to the maintainer. The Android WebView must then be updated to use that URL as its home address and treat its host as internal navigation.

Do not change `main.py`, Flask routes, login, CAPTCHA, download logic, or Render environment variables for this gateway setup.

## Important

- The Worker has a fixed upstream; it is not an arbitrary/open proxy.
- Render remains the Python execution host. The gateway hides the Render hostname from the Android app's configured entry URL, but does not migrate Python off Render.
- Do not cache authenticated pages, session responses, or downloads. The Worker sends `cache-control: no-store`.
- Test login, CAPTCHA, queue/download endpoints, redirects, and file downloads through the Worker before distributing a new APK.
