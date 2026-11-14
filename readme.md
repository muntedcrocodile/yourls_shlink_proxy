# YOURLS → Shlink URL shortener proxy

A small Flask app that speaks the [YOURLS API](https://yourls.org/docs/development/api)
(plus PrivateBin's simple `?link=` style) and creates the short URLs in
[Shlink](https://shlink.io). It's built for PrivateBin, but any YOURLS client works.

## Endpoints

| Request | Response |
|---|---|
| `GET /?link=<url-encoded URL>` | The short URL as `text/plain`. On error, a non-2xx status with a plain-text message that contains no URL. This is what PrivateBin's `urlshortener = "https://proxy.example/?link="` calls from the browser (CORS enabled). |
| `GET\|POST /yourls-api.php` (or `/` with `action=`) | YOURLS API. Actions: `shorturl` (`url`, `keyword`, `title`), `expand` and `url-stats` (`shorturl` = keyword or full short URL), `version`, and, only when `YOURLS_SIGNATURE` is set, `stats` (`filter` = top/bottom/last, `limit`) and `db-stats`. `format` = `xml` (default), `json`, `jsonp` (`callback`) or `simple`. |
| `GET /healthz` | `ok`. Add `?deep=1` to also check Shlink's `/rest/health` (503 if it's down). |

The HTTP status follows YOURLS: the `statusCode`/`errorCode` of the result. Shlink
failures come back as 502 (unreachable, bad API key, unexpected reply) or 504
(timeout). The proxy never makes up a short URL.

Differences from real YOURLS:
- Duplicate long URLs return the existing short URL with `status: success` rather than
  `error:url`.
- Keywords may only use `A-Z a-z 0-9 - _`.
- `username`/`password` auth isn't supported; use the signature.
- The success `message` doesn't repeat the long URL.

## Configuration (environment)

| Variable | Default | |
|---|---|---|
| `SHLINK_URL` | *required* | Shlink base URL (`http://shlink:8080`) **or** the full `…/rest/vN/short-urls` endpoint. On the same Docker network, use the internal address. |
| `SHLINK_API_KEY` | *required* | |
| `SHLINK_API_VERSION` | from URL, else `3` | Shlink 3–5 serve v1–v3. |
| `SHLINK_TIMEOUT` | `5` | Seconds, total for each client request (all Shlink calls together). A single stalled read can overrun by up to this much again, so keep it at or below half of PrivateBin's 10 s client timeout. |
| `ALLOWED_URL_PREFIXES` | *(any)* | Comma-separated. Only these URLs can be shortened, e.g. `https://bin.example.org/?`. **Recommended:** the `?link=` endpoint is called from browsers, so it can't hold a secret. |
| `YOURLS_SIGNATURE` | *(open)* | YOURLS signature token, at least 16 characters. It's required on every API action: `signature=<token>`, or `timestamp=<unix>&signature=<sha256(timestamp+token)>` with optional `hash=sha512` etc. Use it with PrivateBin's server-side `[yourls]` section. |
| `YOURLS_SIGNATURE_TTL` | `43200` | Validity window for timestamped signatures. |
| `LINK_REQUIRES_SIGNATURE` | `false` | Require the signature on `?link=` too. |
| `SHLINK_MAX_VISITS` | *(unlimited)* | Earlier versions hard-coded `100`. |
| `SHLINK_TAGS` | `privatebin` | Comma-separated. |
| `SHLINK_DOMAIN` | *(Shlink default)* | |
| `SHLINK_CRAWLABLE` | `false` | |
| `SHLINK_FIND_IF_EXISTS` | `true` | Reuse the existing short URL for the same long URL, unless that one is used up or expired. |
| `CORS_ALLOW_ORIGINS` | `*` | `*`, a comma-separated list of origins, or empty to disable CORS. |
| `ALLOWED_URL_SCHEMES` | `http,https` | Shlink itself accepts `javascript:` and similar. |
| `MAX_URL_LENGTH` | `8192` | |
| `WEB_CONCURRENCY` | `2` | gunicorn workers. |

A missing or invalid setting stops the app at startup with a clear error. If neither
`YOURLS_SIGNATURE` nor `ALLOWED_URL_PREFIXES` is set, the app logs a warning: in that
case anyone who can reach it can create short links on your domain.

## Running

```bash
cp .env.example .env   # fill in SHLINK_URL / SHLINK_API_KEY (+ ALLOWED_URL_PREFIXES)
docker compose up -d --build
curl "http://127.0.0.1:8080/?link=$(python3 -c 'import urllib.parse;print(urllib.parse.quote("https://bin.example.org/?abc#key", safe=""))')"
```

Compose publishes on `127.0.0.1:8080`. Expose it through your reverse proxy.

### PrivateBin

* **Browser-side (current setup):** `urlshortener = "https://proxy.example/?link="`, with
  `ALLOWED_URL_PREFIXES=https://bin.example.org/?` set on the proxy.
* **Server-side YOURLS proxy (PrivateBin ≥ 1.5):** `urlshortener = "${basepath}?shortenviayourls&link="`
  and `[yourls] apiurl = "https://proxy.example/yourls-api.php"`, `signature = "<YOURLS_SIGNATURE>"`.
* PrivateBin ≥ 2.0.1 can also talk to Shlink directly (`[shlink]` section and
  `shortenviashlink`), which makes this proxy unnecessary.

**Privacy:** a PrivateBin paste URL carries its decryption key in the `#fragment`.
Shortening sends the full URL to the proxy and stores it in Shlink. The proxy never logs
long URLs or query strings; its container access log records the path only. Configure
your reverse proxy the same way: don't log `$request`/`$args` for this vhost.

## Development

```bash
make test   # unit tests, Shlink mocked (no network)
make e2e    # real Shlink in Docker on 127.0.0.1 + image build/smoke test
SHLINK_IMAGE=shlinkio/shlink:4 make e2e   # against another Shlink version
```

# Contributions

Contributions are welcome! Please open a pull request or an issue to discuss changes to the project.

# Donations

**XMR:**
`83NpmSwz69tAUYtMeUgN2rCRDfzFSsrRzAsw883dvMAU1qERF385ZtKMLTMdu19uqyN3vQQzDVcWJD1mRmnVwsckPZnYY7a`.
