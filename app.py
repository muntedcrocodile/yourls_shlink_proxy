from flask import Flask, request, Response
import requests
import logging
import os

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("yourls_shlink_proxy")

# Shlink API details
SHLINK_URL = os.environ.get('SHLINK_URL')
SHLINK_API_KEY = os.environ.get('SHLINK_API_KEY')
# Seconds to wait for Shlink. Keep below PrivateBin's 10s client-side abort and
# gunicorn's worker timeout so the client gets a clean error, not a dropped socket.
SHLINK_TIMEOUT = float(os.environ.get('SHLINK_TIMEOUT', '8'))


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


def shlink_create(long_url):
    headers = {
        'Content-Type': 'application/json',
        'X-Api-Key': SHLINK_API_KEY,
    }
    request_body = {
        'longUrl': long_url,
        'maxVisits': 100,
        'tags': ['PrivateBin'],
        'crawlable': False,
    }
    try:
        response = requests.post(SHLINK_URL, json=request_body, headers=headers,
                                 timeout=SHLINK_TIMEOUT)
    except requests.Timeout:
        raise ShlinkError(504, 'timeout', 'Timed out waiting for Shlink')
    except requests.RequestException as exc:
        # Log the exception class only: the message can include the URL.
        log.error('cannot reach Shlink: %s', type(exc).__name__)
        raise ShlinkError(502, 'unreachable', 'Cannot reach Shlink')
    return _parse_shlink_response(response)


def _text(body, status=200):
    return Response(body, status=status, mimetype='text/plain')


@app.route("/", methods=["GET"])
def translate():

    link = request.args.get('link')

    try:
        short = shlink_create(link)
    except ShlinkError as exc:
        log.warning('shorten failed: status=%s kind=%s', exc.status, exc.kind)
        # Never put a URL in an error body: PrivateBin extracts the first URL
        # it finds and would present it as the "short" link.
        return _text(f'Error: {exc.detail}'.replace('://', ' '), exc.status)

    logging.info(short)

    # Return the shortUrl from the response
    return _text(short['shortUrl'])


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
