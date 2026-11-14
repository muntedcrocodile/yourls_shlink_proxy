from dataclasses import dataclass
from datetime import datetime, timezone
from flask import Flask, Response, current_app, g, request
import requests
import logging
import os
import re
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape as xml_escape
import hashlib
import hmac
import json
import time

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("yourls_shlink_proxy")

_ENDPOINT_RE = re.compile(r'^(?P<base>.*?)/rest/v(?P<version>\d+)(?:/short-urls)?/?$')


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    # Shlink server base URL, e.g. https://s.example or http://shlink:8080
    shlink_base: str
    shlink_api_key: str
    shlink_api_version: str = '3'
    # Seconds to wait for Shlink. Keep below PrivateBin's 10s client-side abort and
    # gunicorn's worker timeout so the client gets a clean error, not a dropped socket.
    shlink_timeout: float = 5.0
    # Shlink happily stores javascript:, data:, file: ... URLs; only forward these.
    allowed_url_schemes: frozenset = frozenset({'http', 'https'})
    max_url_length: int = 8192
    # Options for created short URLs. max_visits=None means unlimited.
    max_visits: int | None = None
    tags: tuple = ('privatebin',)
    domain: str | None = None
    crawlable: bool = False
    # Return the existing short URL when the same long URL was shortened before
    # (Shlink findIfExists) instead of creating a new one every time.
    find_if_exists: bool = True
    # Browser clients (PrivateBin >= 1.3.1 fetch()es the shortener from the
    # page) need CORS. '*' or a list of exact origins; empty disables CORS.
    cors_allow_origins: tuple = ('*',)
    # Shared secret for the YOURLS API ("signature token"). When unset the API
    # is open, as the original proxy was. Strongly recommended.
    signature: str | None = None
    signature_ttl: int = 43200          # YOURLS_NONCE_LIFE default (12h)
    # Also require the signature on the legacy ?link= endpoint. Browser
    # clients (PrivateBin's urlshortener) cannot keep a secret, so restrict
    # those with allowed_url_prefixes instead.
    link_requires_signature: bool = False
    # Only URLs starting with one of these prefixes may be shortened, e.g.
    # "https://bin.example.org/?". Empty = any http(s) URL.
    allowed_url_prefixes: tuple = ()

    @property
    def short_urls_endpoint(self):
        return f'{self.shlink_base}/rest/v{self.shlink_api_version}/short-urls'

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        url = (env.get('SHLINK_URL') or '').strip()
        key = (env.get('SHLINK_API_KEY') or '').strip()
        missing = [n for n, v in (('SHLINK_URL', url), ('SHLINK_API_KEY', key)) if not v]
        if missing:
            raise ConfigError(f'missing required environment variable(s): {", ".join(missing)}')
        base, version = parse_shlink_url(url, env.get('SHLINK_API_VERSION'))

        def num(name, default, conv):
            raw = env.get(name)
            try:
                return conv(raw) if raw not in (None, '') else default
            except ValueError:
                raise ConfigError(f'{name} must be a number, got {raw!r}')

        timeout = num('SHLINK_TIMEOUT', 5.0, float)
        if timeout <= 0:
            raise ConfigError('SHLINK_TIMEOUT must be > 0')
        schemes = frozenset(s.strip().lower() for s in
                            env.get('ALLOWED_URL_SCHEMES', 'http,https').split(',') if s.strip())
        max_visits = num('SHLINK_MAX_VISITS', None, int)
        if max_visits is not None and max_visits < 1:
            raise ConfigError('SHLINK_MAX_VISITS must be >= 1 (leave empty for unlimited)')
        tags = tuple(t.strip() for t in env.get('SHLINK_TAGS', 'privatebin').split(',') if t.strip())
        cors = tuple(o.strip().rstrip('/') for o in
                     env.get('CORS_ALLOW_ORIGINS', '*').split(',') if o.strip())
        prefixes = tuple(p.strip() for p in env.get('ALLOWED_URL_PREFIXES', '').split(',') if p.strip())
        for prefix in prefixes:
            pp = urlsplit(prefix)
            if pp.scheme not in ('http', 'https') or not pp.netloc:
                raise ConfigError(f'ALLOWED_URL_PREFIXES entries must be http(s) URLs, got {prefix!r}')
        signature = (env.get('YOURLS_SIGNATURE') or '').strip() or None
        if signature is not None and len(signature) < 16:
            raise ConfigError('YOURLS_SIGNATURE must be at least 16 characters')
        link_signed = _bool(env.get('LINK_REQUIRES_SIGNATURE'), False)
        if link_signed and not signature:
            raise ConfigError('LINK_REQUIRES_SIGNATURE needs YOURLS_SIGNATURE')
        ttl = num('YOURLS_SIGNATURE_TTL', 43200, int)
        if ttl <= 0:
            raise ConfigError('YOURLS_SIGNATURE_TTL must be > 0')
        max_len = num('MAX_URL_LENGTH', 8192, int)
        if max_len < 16:
            raise ConfigError('MAX_URL_LENGTH must be >= 16')
        if not schemes:
            raise ConfigError('ALLOWED_URL_SCHEMES must list at least one scheme')
        return cls(
            signature=signature,
            signature_ttl=ttl,
            link_requires_signature=link_signed,
            allowed_url_prefixes=prefixes,
            cors_allow_origins=cors,
            max_visits=max_visits,
            tags=tags,
            domain=(env.get('SHLINK_DOMAIN') or '').strip() or None,
            crawlable=_bool(env.get('SHLINK_CRAWLABLE'), False),
            find_if_exists=_bool(env.get('SHLINK_FIND_IF_EXISTS'), True),
            shlink_base=base,
            shlink_api_key=key,
            shlink_api_version=version,
            shlink_timeout=timeout,
            allowed_url_schemes=schemes,
            max_url_length=max_len,
        )


def _bool(raw, default):
    if raw is None or raw.strip() == '':
        return default
    v = raw.strip().lower()
    if v in ('1', 'true', 'yes', 'on'):
        return True
    if v in ('0', 'false', 'no', 'off'):
        return False
    raise ConfigError(f'expected a boolean, got {raw!r}')


def parse_shlink_url(url, version=None):
    """Accept a Shlink base URL or a full .../rest/vN/short-urls endpoint.

    The original README asked for the full endpoint, so both forms are
    supported. Returns (base_url_without_trailing_slash, api_version).
    """
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        raise ConfigError(f'SHLINK_URL must be an http(s) URL, got {url!r}')
    if parts.query or parts.fragment:
        raise ConfigError('SHLINK_URL must not contain a query string or fragment')
    if '@' in parts.netloc:
        # Would end up in the startup log; Shlink authenticates with the API key.
        raise ConfigError('SHLINK_URL must not contain credentials')
    path = parts.path.rstrip('/')
    m = _ENDPOINT_RE.match(path)
    if m:
        path, found_version = m.group('base'), m.group('version')
        version = version or found_version
    version = str(version or '3').lstrip('vV')
    if not version.isdigit():
        raise ConfigError(f'SHLINK_API_VERSION must be a number, got {version!r}')
    return f'{parts.scheme}://{parts.netloc}{path}', version


def cfg() -> Config:
    return current_app.config['PROXY']


class ShlinkError(Exception):
    """Shlink could not create/return a short URL.

    `status` is the HTTP status the proxy should answer with, `kind` the
    Shlink problem type (last path segment, lower-cased, e.g. "non-unique-slug"),
    `detail` a human readable message that is safe to show to the client.
    """

    def __init__(self, status, kind, detail):
        super().__init__(detail)
        self.status = status
        self.kind = kind
        self.detail = detail


def _problem_kind(problem):
    raw = str(problem.get('type') or '')
    return raw.rstrip('/').rsplit('/', 1)[-1].lower().replace('_', '-')


def _parse_shlink_response(response, body, expect=None):
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if response.ok and isinstance(data, dict) and (expect is None or data.get(expect)):
        return data
    if not isinstance(data, dict):
        raise ShlinkError(502, 'bad-response',
                          f'Unexpected response from Shlink (HTTP {response.status_code})')
    kind = _problem_kind(data)
    if response.status_code in (401, 403):
        # Don't leak whether/which key is configured; this is an operator problem.
        raise ShlinkError(502, kind or 'invalid-api-key', 'Proxy is not authorised against Shlink')
    detail = data.get('detail') or data.get('title') or f'Shlink returned HTTP {response.status_code}'
    status = 400 if response.status_code in (400, 422) else 502
    raise ShlinkError(status, kind or 'error', str(detail))


def validate_long_url(url):
    """Return the cleaned URL or raise ShlinkError(400, ...)."""
    url = (url or '').strip()
    if not url:
        raise ShlinkError(400, 'nourl', 'Missing or malformed URL')
    if len(url) > cfg().max_url_length:
        raise ShlinkError(400, 'nourl', f'URL is longer than {cfg().max_url_length} characters')
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ShlinkError(400, 'nourl', 'Missing or malformed URL')
    if (parts.scheme.lower() not in cfg().allowed_url_schemes or not parts.netloc
            or any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7f for c in url)):
        raise ShlinkError(400, 'nourl', 'Missing or malformed URL')
    prefixes = cfg().allowed_url_prefixes
    if prefixes and not any(_has_prefix(url, p) for p in prefixes):
        raise ShlinkError(400, 'forbidden-url', 'This URL may not be shortened here')
    return url


def _has_prefix(url, prefix):
    # Compare scheme and host case-insensitively, the rest exactly, and make
    # sure "https://bin.example" does not match "https://bin.example.evil.com".
    u, p = urlsplit(url), urlsplit(prefix)
    if (u.scheme.lower(), u.netloc.lower()) != (p.scheme.lower(), p.netloc.lower()):
        return False
    rest = url.split(u.netloc, 1)[1]
    prefix_rest = prefix.split(p.netloc, 1)[1]
    if not rest.startswith(prefix_rest):
        return False
    # Browsers resolve "..", "%2e%2e" and "\\" in the path when following the
    # redirect, which would step outside a path prefix such as "/bin/".
    segments = unquote(u.path).split('/')
    return '\\' not in u.path and '.' not in segments and '..' not in segments


# ---- authentication -------------------------------------------------------

_HASHES = {'md5': hashlib.md5, 'sha1': hashlib.sha1, 'sha256': hashlib.sha256,
           'sha384': hashlib.sha384, 'sha512': hashlib.sha512}


def check_signature(params, now=None):
    """YOURLS passwordless API auth.

    Accepts either `signature=<token>` or `timestamp=<unix>&signature=<hash>`
    where hash = H(timestamp + token) or H(token + timestamp) and H is
    `hash` (sha256 default as in YOURLS 1.9; md5 for older clients when the
    signature is 32 hex chars and no `hash` is given).
    """
    secret = cfg().signature
    given = (params.get('signature') or '').strip()
    if not secret or not given:
        return False
    timestamp = (params.get('timestamp') or '').strip()
    if not timestamp:
        return hmac.compare_digest(given.encode(), secret.encode())
    if not re.fullmatch(r'[0-9]{1,12}', timestamp):
        return False
    ts = int(timestamp)
    now = time.time() if now is None else now
    if abs(now - ts) >= cfg().signature_ttl:
        return False
    algo = (params.get('hash') or '').strip().lower() or ('md5' if len(given) == 32 else 'sha256')
    fn = _HASHES.get(algo)
    if fn is None:
        return False
    given = given.lower()
    return any(hmac.compare_digest(given, fn(m.encode()).hexdigest())
               for m in (timestamp + secret, secret + timestamp))


def _auth_error(params):
    msg = 'Invalid username or password' if ('username' in params or 'password' in params) \
        else 'Please log in'
    return _api_error(403, msg)


def _shlink_headers():
    return {
        'Accept': 'application/json',
        'X-Api-Key': cfg().shlink_api_key,
    }


def shlink_call(method, path, *, expect=None, **kwargs):
    """Call the Shlink REST API and return the decoded JSON body.

    Raises ShlinkError for transport errors and Shlink problem responses.
    `expect` names a key that must be present in a successful response.
    """
    c = cfg()
    url = f'{c.shlink_base}/rest/v{c.shlink_api_version}/{path}'
    # SHLINK_TIMEOUT is a budget for the whole client request, shared by all
    # Shlink calls it makes. requests' own timeout is per socket operation,
    # so a slow-dripping upstream could otherwise exceed it many times over.
    deadline = g.setdefault('shlink_deadline', time.monotonic() + c.shlink_timeout)
    timed_out = ShlinkError(504, 'timeout', 'Timed out waiting for Shlink')
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise timed_out
    try:
        with requests.request(method, url, headers=_shlink_headers(), timeout=remaining,
                              stream=True, **kwargs) as response:
            body = bytearray()
            while True:
                # read1() returns whatever has arrived instead of waiting for
                # a full buffer, so the deadline is checked between reads.
                chunk = response.raw.read1(16384, decode_content=True)
                if not chunk:
                    break
                body += chunk
                if time.monotonic() > deadline:
                    raise timed_out
                if len(body) > MAX_SHLINK_RESPONSE:
                    raise ShlinkError(502, 'bad-response', 'Response from Shlink is too large')
    except requests.Timeout:
        raise timed_out
    except requests.RequestException as exc:
        # Log the exception class only: the message can include the URL.
        log.error('cannot reach Shlink: %s', type(exc).__name__)
        raise ShlinkError(502, 'unreachable', 'Cannot reach Shlink')
    return _parse_shlink_response(response, bytes(body), expect)


MAX_SHLINK_RESPONSE = 2 * 1024 * 1024


def shlink_create(long_url, slug=None, title=None):
    c = cfg()
    request_body = {
        'longUrl': long_url,
        'tags': list(c.tags),
        'crawlable': c.crawlable,
    }
    if c.max_visits is not None:
        request_body['maxVisits'] = c.max_visits
    if c.domain:
        request_body['domain'] = c.domain
    if slug:
        request_body['customSlug'] = slug
    if title:
        request_body['title'] = title
    if c.find_if_exists and not slug:
        # Shlink only reuses a short URL whose options all match this request.
        short = shlink_call('POST', 'short-urls', json={**request_body, 'findIfExists': True},
                            expect='shortUrl')
        if not _exhausted(short):
            return short
        log.info('existing short URL %s is exhausted; creating a new one', short.get('shortCode'))
    return shlink_call('POST', 'short-urls', json=request_body, expect='shortUrl')


def _exhausted(short):
    """True if an existing short URL no longer redirects (max visits / expiry)."""
    meta = short.get('meta') or {}
    max_visits = meta.get('maxVisits')
    if max_visits and _visits(short) >= int(max_visits):
        return True
    valid_until = meta.get('validUntil')
    if valid_until:
        try:
            if datetime.fromisoformat(valid_until) <= datetime.now(timezone.utc):
                return True
        except ValueError:
            pass
    return False


def shlink_lookup(short):
    """Resolve a keyword or full short URL to Shlink's short URL record."""
    short = (short or '').strip()
    domain = None
    if '://' in short:
        try:
            parts = urlsplit(short)
        except ValueError:
            raise ShlinkError(404, 'not-found', 'Error: could not find keyword')
        domain, short = parts.netloc, parts.path
    code = short.strip('/')
    if not code:
        raise ShlinkError(404, 'not-found', 'Error: could not find keyword')
    params = {'domain': domain} if domain else None
    try:
        return shlink_call('GET', 'short-urls/' + quote(code, safe=''), params=params, expect='shortUrl')
    except ShlinkError as exc:
        if exc.status == 400 or exc.kind in ('short-url-not-found', 'invalid-shortcode'):
            raise ShlinkError(404, 'not-found', 'Error: could not find keyword')
        raise


def _visits(short):
    summary = short.get('visitsSummary')
    if isinstance(summary, dict):
        return int(summary.get('total') or 0)
    return int(short.get('visitsCount') or 0)   # Shlink < 3.4


def _yourls_date(iso):
    try:
        return datetime.fromisoformat(iso).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return iso or ''


def _text(body, status=200):
    return Response(body, status=status, mimetype='text/plain')


def translate():
    """Legacy endpoint: GET /?link=<url> -> short URL as text/plain.

    This is what PrivateBin's `urlshortener = "https://proxy/?link="` uses.
    """
    link = request.values.get('link')
    if cfg().link_requires_signature and not check_signature(request.values):
        return _text('Error: Please log in', 403)

    try:
        short = shlink_create(validate_long_url(link))
    except ShlinkError as exc:
        log.warning('shorten failed: status=%s kind=%s', exc.status, exc.kind)
        # Never put a URL in an error body: PrivateBin extracts the first URL
        # it finds and would present it as the "short" link.
        return _text(f'Error: {exc.detail}'.replace('://', ' '), exc.status)

    # Log the short code only. The long URL of a PrivateBin paste carries the
    # decryption key in its #fragment and must never reach the logs.
    log.info('shortened: code=%s', short.get('shortCode'))

    # Return the shortUrl from the response
    return _text(short['shortUrl'])


# --------------------------------------------------------------------------
# YOURLS API emulation (yourls-api.php)
# https://yourls.org/docs/development/api
# --------------------------------------------------------------------------

YOURLS_VERSION = '1.9.2'   # version reported by action=version
_KEYWORD_RE = re.compile(r'^[A-Za-z0-9_-]{1,100}$')
_JSONP_CALLBACK_RE = re.compile(r'^[A-Za-z_$][0-9A-Za-z_$.\[\]]{0,99}$')
_XML_NAME_RE = re.compile(r'[^A-Za-z0-9_.-]')
# Characters not allowed in XML 1.0 documents (e.g. from a Shlink title).
_XML_INVALID_RE = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff\ud800-\udfff]')


def _api_error(status, message, **extra):
    return {'errorCode': str(status), 'message': message, 'simple': message,
            'statusCode': status, **extra}


def api_shorturl(params):
    try:
        url = validate_long_url(params.get('url'))
    except ShlinkError as exc:
        return {'status': 'fail', 'code': 'error:nourl', 'message': exc.detail,
                'errorCode': '400', 'statusCode': 400, 'simple': ''}
    keyword = (params.get('keyword') or '').strip()
    title = (params.get('title') or '').strip() or None
    if keyword and not _KEYWORD_RE.match(keyword):
        return {'status': 'fail', 'code': 'error:keyword', 'statusCode': 400, 'errorCode': '400',
                'message': 'Keywords may only contain letters, digits, "-" and "_"', 'simple': ''}
    try:
        short = shlink_create(url, slug=keyword or None, title=title)
    except ShlinkError as exc:
        if exc.kind in ('non-unique-slug', 'invalid-slug'):
            # YOURLS reports a taken keyword as a failed but valid request.
            msg = f'Short URL {keyword} already exists in database or is reserved'
            return {'status': 'fail', 'code': 'error:keyword', 'message': msg,
                    'statusCode': 200, 'simple': ''}
        if exc.status == 400:
            return {'status': 'fail', 'code': 'error:nourl', 'message': exc.detail,
                    'errorCode': '400', 'statusCode': 400, 'simple': ''}
        raise
    log.info('shortened: code=%s', short.get('shortCode'))
    record = {
        'keyword': short.get('shortCode'),
        'url': short.get('longUrl', url),
        'title': short.get('title') or '',
        'date': _yourls_date(short.get('dateCreated')),
        'ip': '',
    }
    # The message deliberately does not echo the long URL (YOURLS does): browser
    # clients such as PrivateBin pick the *shortest* URL found in the body.
    return {'status': 'success', 'code': '', 'url': record,
            'message': 'Short URL added to database', 'title': record['title'],
            'shorturl': short['shortUrl'], 'statusCode': 200, 'simple': short['shortUrl']}


def api_expand(params):
    shorturl = params.get('shorturl')
    try:
        short = shlink_lookup(shorturl)
    except ShlinkError as exc:
        if exc.status == 404:
            return _api_error(404, exc.detail, keyword=(shorturl or '').strip())
        raise
    return {'keyword': short.get('shortCode'), 'shorturl': short['shortUrl'],
            'longurl': short.get('longUrl'), 'title': short.get('title') or '',
            'simple': short.get('longUrl'), 'message': 'success', 'statusCode': 200}


def _link_record(short):
    return {'shorturl': short['shortUrl'], 'url': short.get('longUrl'),
            'title': short.get('title') or '', 'timestamp': _yourls_date(short.get('dateCreated')),
            'ip': '', 'clicks': _visits(short)}


def api_url_stats(params):
    shorturl = params.get('shorturl')
    try:
        short = shlink_lookup(shorturl)
    except ShlinkError as exc:
        if exc.status == 404:
            return _api_error(404, exc.detail)
        raise
    return {'statusCode': 200, 'message': 'success', 'link': _link_record(short)}


def api_version(params):
    return {'version': YOURLS_VERSION, 'simple': YOURLS_VERSION, 'statusCode': 200}


def _totals():
    page = shlink_call('GET', 'short-urls', params={'itemsPerPage': 1}, expect='shortUrls')
    total_links = int(page['shortUrls'].get('pagination', {}).get('totalItems') or 0)
    visits = shlink_call('GET', 'visits', expect='visits')['visits']
    if 'nonOrphanVisits' in visits:
        total_clicks = int(visits['nonOrphanVisits'].get('total') or 0)
    else:                                           # Shlink < 3.5
        total_clicks = int(visits.get('visitsCount') or 0)
    return {'total_links': total_links, 'total_clicks': total_clicks}


def api_db_stats(params):
    return {'db-stats': _totals(), 'statusCode': 200, 'message': 'success'}


_STATS_ORDER = {'top': 'visits-DESC', 'bottom': 'visits-ASC', 'last': 'dateCreated-DESC',
                'rand': 'dateCreated-DESC'}


def api_stats(params):
    filt = (params.get('filter') or 'top').lower()
    if filt not in _STATS_ORDER:
        filt = 'top'
    try:
        limit = max(1, min(int(params.get('limit') or 10), 100))
    except ValueError:
        limit = 10
    page = shlink_call('GET', 'short-urls', expect='shortUrls',
                       params={'itemsPerPage': limit, 'orderBy': _STATS_ORDER[filt]})
    links = {f'link_{i}': _link_record(s) for i, s in enumerate(page['shortUrls'].get('data', []), 1)}
    return {'links': links, 'stats': _totals(), 'statusCode': 200, 'message': 'success'}


API_ACTIONS = {
    'shorturl': api_shorturl,
    'expand': api_expand,
    'url-stats': api_url_stats,
    'version': api_version,
    'db-stats': api_db_stats,
    'stats': api_stats,
}
# These list every stored long URL (PrivateBin URLs include decryption keys),
# so they are only served when API authentication is configured.
PRIVATE_ACTIONS = {'stats', 'db-stats'}


def _xml_encode(value, name='result'):
    tag = _XML_NAME_RE.sub('_', str(name)) or 'item'
    if tag[0].isdigit() or tag[0] in '.-':
        tag = 'item_' + tag
    if isinstance(value, dict):
        inner = ''.join(_xml_encode(v, k) for k, v in value.items())
    elif isinstance(value, (list, tuple)):
        inner = ''.join(_xml_encode(v, 'item') for v in value)
    elif value is None:
        inner = ''
    elif isinstance(value, bool):
        inner = 'true' if value else 'false'
    else:
        inner = xml_escape(_XML_INVALID_RE.sub('\ufffd', str(value)))
    return f'<{tag}>{inner}</{tag}>'


def api_output(fmt, result, callback=''):
    """Render like yourls_api_output(): HTTP status from statusCode/errorCode."""
    result = dict(result)
    simple = result.pop('simple', '')
    status = result.get('statusCode') or result.get('errorCode') or 200
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 200
    fmt = (fmt or 'xml').lower()
    if fmt == 'jsonp':
        if not _JSONP_CALLBACK_RE.match(callback or ''):
            err = _api_error(400, 'Invalid JSONP callback')
            err.pop('simple')
            return Response(json.dumps(err), status=400, mimetype='application/json')
        return Response(f'/**/{callback}({json.dumps(result)});', status=status,
                        mimetype='application/javascript')
    if fmt == 'json':
        return Response(json.dumps(result), status=status, mimetype='application/json')
    if fmt == 'xml':
        body = '<?xml version="1.0" encoding="UTF-8"?>\n' + _xml_encode(result)
        return Response(body, status=status, mimetype='application/xml')
    return Response('' if simple is None else str(simple), status=status, mimetype='text/plain')


def yourls_api():
    params = request.values
    action = (params.get('action') or '').strip()
    handler = API_ACTIONS.get(action)
    try:
        if cfg().signature and not check_signature(params):
            log.warning('api %s: authentication failed', action or '-')
            result = _auth_error(params)
        elif action in PRIVATE_ACTIONS and not cfg().signature:
            result = _api_error(403, f'Action "{action}" requires YOURLS_SIGNATURE to be configured')
        elif handler is None:
            result = _api_error(400, 'Unknown or missing "action" parameter')
        else:
            result = handler(params)
    except ShlinkError as exc:
        log.warning('api %s failed: status=%s kind=%s', action, exc.status, exc.kind)
        result = _api_error(exc.status, exc.detail.replace('://', ' '))
    return api_output(params.get('format'), result, params.get('callback', ''))


def healthz():
    """Liveness; with ?deep=1 also checks that Shlink answers /rest/health."""
    if request.args.get('deep') not in ('1', 'true'):
        return _text('ok')
    c = cfg()
    try:
        r = requests.get(f'{c.shlink_base}/rest/health', timeout=c.shlink_timeout)
        ok = r.ok and r.json().get('status') == 'pass'
    except (requests.RequestException, ValueError, AttributeError):
        ok = False
    return _text('ok' if ok else 'shlink unavailable', 200 if ok else 503)


def index():
    # `/` serves both the legacy ?link= endpoint and the YOURLS API.
    if 'action' not in request.values and 'link' in request.values:
        return translate()
    return yourls_api()


def add_cors_headers(response):
    allowed = cfg().cors_allow_origins
    if not allowed:
        return response
    origin = request.headers.get('Origin')
    if '*' in allowed:
        response.headers['Access-Control-Allow-Origin'] = '*'
    elif origin and origin.rstrip('/') in allowed:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.vary.add('Origin')
    else:
        response.vary.add('Origin')
        return response
    if request.method == 'OPTIONS':
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Accept'
        response.headers['Access-Control-Max-Age'] = '86400'
    return response


def create_app(config=None):
    """Application factory. Raises ConfigError if configuration is invalid."""
    config = config or Config.from_env()
    app = Flask(__name__)
    app.config['PROXY'] = config
    app.add_url_rule('/', 'index', index, methods=['GET', 'POST'])
    app.add_url_rule('/yourls-api.php', 'yourls_api', yourls_api, methods=['GET', 'POST'])
    app.add_url_rule('/healthz', 'healthz', healthz, methods=['GET'])
    app.after_request(add_cors_headers)
    log.info('proxying to Shlink %s (API v%s)', config.shlink_base, config.shlink_api_version)
    if not config.signature and not config.allowed_url_prefixes:
        log.warning('neither YOURLS_SIGNATURE nor ALLOWED_URL_PREFIXES is set: anyone who can '
                    'reach this proxy can create short URLs for arbitrary sites')
    return app


def __getattr__(name):
    # Keep `gunicorn app:app` working while failing fast with a clear message
    # when the configuration is incomplete.
    if name == 'app':
        globals()['app'] = create_app()
        return globals()['app']
    raise AttributeError(name)


if __name__ == '__main__':
    create_app().run(host='127.0.0.1', port=8080)
