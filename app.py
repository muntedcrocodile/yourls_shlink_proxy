from dataclasses import dataclass, field
from flask import Flask, current_app, request, Response
import requests
import logging
import os
import re
from urllib.parse import urlsplit

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
    shlink_timeout: float = 8.0
    # Shlink happily stores javascript:, data:, file: ... URLs; only forward these.
    allowed_url_schemes: frozenset = frozenset({'http', 'https'})
    max_url_length: int = 8192
    # Options for created short URLs. max_visits=None means unlimited.
    max_visits: int | None = None
    tags: tuple = ('privatebin',)
    domain: str | None = None
    crawlable: bool = False

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

        timeout = num('SHLINK_TIMEOUT', 8.0, float)
        if timeout <= 0:
            raise ConfigError('SHLINK_TIMEOUT must be > 0')
        schemes = frozenset(s.strip().lower() for s in
                            env.get('cfg().allowed_url_schemes', 'http,https').split(',') if s.strip())
        max_visits = num('SHLINK_MAX_VISITS', None, int)
        if max_visits is not None and max_visits < 1:
            raise ConfigError('SHLINK_MAX_VISITS must be >= 1 (leave empty for unlimited)')
        tags = tuple(t.strip() for t in env.get('SHLINK_TAGS', 'privatebin').split(',') if t.strip())
        return cls(
            max_visits=max_visits,
            tags=tags,
            domain=(env.get('SHLINK_DOMAIN') or '').strip() or None,
            crawlable=_bool(env.get('SHLINK_CRAWLABLE'), False),
            shlink_base=base,
            shlink_api_key=key,
            shlink_api_version=version,
            shlink_timeout=timeout,
            allowed_url_schemes=schemes,
            max_url_length=num('cfg().max_url_length', 8192, int),
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


def _parse_shlink_response(response):
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.ok and isinstance(data, dict) and data.get('shortUrl'):
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
    if parts.scheme.lower() not in cfg().allowed_url_schemes or not parts.netloc or any(c.isspace() for c in url):
        raise ShlinkError(400, 'nourl', 'Missing or malformed URL')
    return url


def _shlink_headers():
    return {
        'Accept': 'application/json',
        'X-Api-Key': cfg().shlink_api_key,
    }


def shlink_create(long_url):
    headers = _shlink_headers()
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
    try:
        response = requests.post(cfg().short_urls_endpoint, json=request_body,
                                 headers=headers, timeout=cfg().shlink_timeout)
    except requests.Timeout:
        raise ShlinkError(504, 'timeout', 'Timed out waiting for Shlink')
    except requests.RequestException as exc:
        # Log the exception class only: the message can include the URL.
        log.error('cannot reach Shlink: %s', type(exc).__name__)
        raise ShlinkError(502, 'unreachable', 'Cannot reach Shlink')
    return _parse_shlink_response(response)


def _text(body, status=200):
    return Response(body, status=status, mimetype='text/plain')


def translate():

    link = request.args.get('link')

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


def create_app(config=None):
    """Application factory. Raises ConfigError if configuration is invalid."""
    config = config or Config.from_env()
    app = Flask(__name__)
    app.config['PROXY'] = config
    app.add_url_rule('/', 'translate', translate, methods=['GET'])
    log.info('proxying to Shlink %s (API v%s)', config.shlink_base, config.shlink_api_version)
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
