"""Keep secrets out of everything the process writes down.

The provider key is part of the provider URL (``…/v2/<key>``), and the URL is
part of the text of every httpx exception. Text like that used to travel from
an exception into ``sync_state.last_error`` — which the status bar renders on
every page and ``/api/status`` returns — and into the log.

There are two layers, on purpose:

1. **Where errors are made.** The provider and the price source describe a
   failure by method, HTTP status and exception type, and never by quoting
   httpx. That is what keeps the useful part of a message.
2. **Where errors are kept.** Whatever reaches the database or the log is
   scrubbed here anyway: :func:`redact` for stored text, and a log-record
   factory for every log line, its arguments and its traceback. A message
   written next year with the URL in it is still caught.

Secrets are registered once, from settings, when the app is built. The
patterns below catch the shapes a key takes in a URL even when it was never
registered — an ``ALCHEMY_URL`` pointing at a proxy, say.
"""

from __future__ import annotations

import logging
import re
import threading

import httpx

MASK = "***"

# Shorter than this and a "secret" would mask ordinary words in the log.
_MIN_SECRET_LENGTH = 8

_secrets: set[str] = set()
_lock = threading.Lock()
_installed = False

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Alchemy JSON-RPC (…/v2/<key>) and Prices (…/prices/v1/<key>), and the
    # same shape on any compatible endpoint.
    (re.compile(r"(/v[23]/)[A-Za-z0-9_\-]{8,}"), rf"\g<1>{MASK}"),
    (re.compile(r"(/prices/v1/)[A-Za-z0-9_\-]{8,}"), rf"\g<1>{MASK}"),
    # Credentials in a query string.
    (
        re.compile(
            r"([?&](?:api[_-]?key|apikey|key|token|access_token)=)[^&\s'\"#]+", re.IGNORECASE
        ),
        rf"\g<1>{MASK}",
    ),
    # Credentials in the authority: scheme://user:password@host
    (re.compile(r"(://[^/\s:@'\"]+:)[^@\s/'\"]+@"), rf"\g<1>{MASK}@"),
)


def describe_failure(exc: Exception) -> tuple[int | None, str]:
    """What went wrong with one request, without quoting httpx.

    httpx puts the request URL — which carries the API key — into the text of
    its exceptions, so that text is never passed on. The HTTP status and the
    exception type are what an operator acts on, and neither can leak.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code, f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return None, f"timeout ({type(exc).__name__})"
    if isinstance(exc, httpx.HTTPError):
        return None, type(exc).__name__
    # The only ValueError in reach is a body that is not JSON.
    return None, f"unreadable response ({type(exc).__name__})"


def register_secret(value: str | None) -> None:
    """Remember a value that must never be written down verbatim."""
    if value and len(value) >= _MIN_SECRET_LENGTH:
        with _lock:
            _secrets.add(value)


def redact(text: str | None) -> str | None:
    """Return ``text`` with every registered secret and every credential-shaped
    URL part replaced by ``***``. ``None`` stays ``None``."""
    if not text:
        return text
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, MASK)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


_PLAIN = (int, float, bool, type(None))


def _scrub_arg(value: object) -> object:
    """A log argument with any secret removed, keeping its type where that
    matters: numbers stay numbers for ``%d``. Anything else — an exception,
    a URL object — is rendered here, because its text is where a key hides."""
    if isinstance(value, _PLAIN):
        return value
    text = value if isinstance(value, str) else str(value)
    return redact(text)


def _scrub(record: logging.LogRecord) -> None:
    # Arguments are scrubbed in place rather than folded into the message:
    # some formatters read them positionally (uvicorn's access log unpacks
    # client, method, path, version and status from record.args).
    if isinstance(record.msg, str):
        record.msg = redact(record.msg)
    else:
        record.msg = redact(str(record.msg))
    if isinstance(record.args, tuple):
        record.args = tuple(_scrub_arg(arg) for arg in record.args)
    elif isinstance(record.args, dict):
        record.args = {key: _scrub_arg(value) for key, value in record.args.items()}

    if record.exc_info and not record.exc_text:
        # Formatters reuse exc_text when it is set, so the traceback is
        # rendered once, here, and scrubbed — type and frames are kept.
        record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
    if record.stack_info:
        record.stack_info = redact(record.stack_info)


def install_log_redaction() -> None:
    """Scrub every log record as it is created, whichever logger or handler
    ends up writing it. Idempotent."""
    global _installed
    with _lock:
        if _installed:
            return
        previous = logging.getLogRecordFactory()

        def factory(*args, **kwargs) -> logging.LogRecord:
            record = previous(*args, **kwargs)
            _scrub(record)
            return record

        logging.setLogRecordFactory(factory)
        _installed = True
