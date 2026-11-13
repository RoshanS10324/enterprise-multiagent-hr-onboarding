"""The UC-02 side of the UC-04 / UC-05 trigger contract: a signed HTTP POST to a URL the owning team supplies.

UC-02 knows no endpoint for UC-04 or UC-05 and invents none. A deployment sets ``UC02_UC04_TRIGGER_URL`` /
``UC02_UC05_TRIGGER_URL`` (https) and a shared secret per use case once those teams publish their endpoint. Until then no
adapter is built and nothing is triggered.

The contract (what the receiving team must implement):

* ``POST <url>`` with a JSON body: the trigger payload (``trigger_id``, ``use_case``, ``case_id``, ``onboarding_id``,
  ``employee_id``, ``client_id``, ``plan_version``, ``correlation_id``, ``configuration_version``,
  ``verified_documents`` (keys only), ``tasks``).
* Header ``X-UC02-Signature``: hex HMAC-SHA256 of the exact request body using the shared secret.
* ``trigger_id`` is the idempotency key: UC-02 delivers at least once, so the receiver must treat a repeated ``trigger_id`` as
  the same trigger and answer 2xx without starting the work again.
* Any 2xx = accepted. Anything else (or no answer) = not accepted; UC-02 retries up to its limit, then flags the use case
  for intervention. A 2xx is an acceptance of the trigger, not a completion: completion arrives as a signed status event.

Only the standard library is used. The URL must be https, redirects are not followed, and nothing sensitive is logged.
"""

from __future__ import annotations

import hmac
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from hashlib import sha256
from typing import Any

from onboarding_task_management.sub_agent.flow import TriggerResult

log = logging.getLogger("uc02.downstream")
SIGNATURE_HEADER = "X-UC02-Signature"

#: ``(url, headers, body) -> (status, parsed json or None)``. Injected in tests.
PostTransport = Callable[[str, Mapping[str, str], bytes], "tuple[int, Any]"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _urllib_post(url: str, headers: Mapping[str, str], body: bytes, timeout: float = 20.0) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:  # noqa: S310 - https only
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        return exc.code, None


def sign_body(secret: bytes, body: bytes) -> str:
    return hmac.new(secret, body, sha256).hexdigest()


class HttpTriggerAdapter:
    """Posts a signed trigger to a team-supplied https URL. Satisfies :class:`flow.TriggerAdapter`."""

    real = True

    def __init__(self, use_case: str, url: str, secret: bytes, *, transport: PostTransport | None = None) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("a downstream trigger URL must be https")
        if not secret:
            raise ValueError("a downstream trigger secret is required")
        self.use_case, self._url, self._secret = use_case, url, secret
        self._post = transport or _urllib_post

    def __repr__(self) -> str:
        return f"<HttpTriggerAdapter {self.use_case} url-configured>"

    def send(self, payload: dict[str, Any]) -> TriggerResult:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", SIGNATURE_HEADER: sign_body(self._secret, body),
                   "Accept": "application/json"}
        try:
            status, parsed = self._post(self._url, headers, body)
        except (OSError, TimeoutError):
            log.warning("downstream.network_error", extra={"use_case": self.use_case})
            return TriggerResult(False, error="network_error")
        log.info("downstream.response", extra={"use_case": self.use_case, "status": status})
        if 200 <= status < 300:
            reference = str((parsed or {}).get("reference", "")) if isinstance(parsed, dict) else ""
            return TriggerResult(True, reference=reference[:120])
        return TriggerResult(False, error=f"http_{status}")


__all__ = ["HttpTriggerAdapter", "SIGNATURE_HEADER", "sign_body"]
