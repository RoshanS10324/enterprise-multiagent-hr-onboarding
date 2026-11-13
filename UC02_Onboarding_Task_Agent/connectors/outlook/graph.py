"""Microsoft Graph implementation of the Outlook connector port. REAL transport; sends nothing unless called.

What it does
* Authenticates to Microsoft Entra ID with the OAuth2 *client credentials* flow (an app registration with a secret)
  and caches the access token in memory only.
* ``create_draft``: saves a message as a DRAFT in the authorised HR mailbox (``POST /users/{mailbox}/messages``).
* ``send``: sends an APPROVED message (``POST /users/{mailbox}/messages/{id}/send``). Graph answers 202 Accepted,
  which means "accepted for sending". It is recorded as ``SUBMITTED``, never as delivered: nothing here has delivery
  evidence.

Safety properties (each is tested against mocked Graph responses; this module has never contacted Microsoft)
* **Approval gate:** ``send`` refuses without ``approval_id`` (``BLOCKED_NO_APPROVAL``), as the port requires.
* **Recipients** come from an approved :class:`RecipientDirectory`; a role with no approved address is an error.
  Nothing is addressed from the message text or from a request.
* **Idempotency:** every draft carries the message's ``idempotency_key`` in a Graph extended property. Before creating
  a draft or sending, the connector looks the key up in Drafts and Sent Items, so a retry, a restart or a second
  process cannot create or send a duplicate. A lost-response retry that finds the message already sent returns
  ``DUPLICATE_IGNORED``.
* **Errors:** 401 refreshes the token once; 429/502/503/504 and network failures are retried with a capped backoff that
  honours ``Retry-After``; 403 and other 4xx are permanent. Failures raise :class:`OutlookConnectorError` with a safe
  message (status and Graph error code only).
* **Logging:** only operation names, HTTP status, attempt numbers and Graph request ids. Never tokens, secrets,
  addresses, subjects or bodies.
* **Hosts:** token and Graph URLs must be https on Microsoft's own hosts, and redirects are not followed, so a bad
  setting cannot send the token or the mail elsewhere.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from onboarding_task_management.sub_agent.connectors.outlook.base import (
    DeliveryReceipt,
    DeliveryStatus,
    OutlookConnectorError,
    OutlookMessage,
)
from onboarding_task_management.sub_agent.connectors.outlook.recipients import (
    RecipientDirectory,
    valid_address,
)

log = logging.getLogger("uc02.outlook.graph")

ENV_TENANT = "UC02_GRAPH_TENANT_ID"
ENV_CLIENT = "UC02_GRAPH_CLIENT_ID"
ENV_SECRET = "UC02_GRAPH_CLIENT_SECRET"
ENV_MAILBOX = "UC02_GRAPH_MAILBOX"
ENV_AUTHORITY = "UC02_GRAPH_AUTHORITY"
ENV_BASE = "UC02_GRAPH_BASE_URL"

LOGIN_HOSTS = frozenset({"login.microsoftonline.com", "login.microsoftonline.us", "login.partner.microsoftonline.cn"})
GRAPH_HOSTS = frozenset({"graph.microsoft.com", "graph.microsoft.us", "dod-graph.microsoft.us",
                         "microsoftgraph.chinacloudapi.cn"})
SCOPE = "https://graph.microsoft.com/.default"
#: Extended property carrying the idempotency key. The GUID is Outlook's public named-property namespace.
KEY_PROPERTY = "String {00020329-0000-0000-C000-000000000046} Name uc02_idempotency_key"
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})

#: ``(method, url, headers, body) -> (status, response headers, parsed JSON or None)``. Injected in tests.
Transport = Callable[[str, str, Mapping[str, str], bytes | None], "tuple[int, Mapping[str, str], Any]"]


class OutlookAuthError(OutlookConnectorError):
    """Authentication or authorisation failed (token refused, or Graph returned 401/403). Not retried blindly."""


class OutlookApiError(OutlookConnectorError):
    """Graph answered with an error. Carries only a status and Graph's error code; never a message body."""


def _host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower()


@dataclass(frozen=True)
class GraphSettings:
    tenant_id: str
    client_id: str
    client_secret: str = field(repr=False)
    mailbox: str
    authority: str = "https://login.microsoftonline.com"
    graph_base: str = "https://graph.microsoft.com/v1.0"
    timeout_s: float = 30.0
    max_attempts: int = 3
    max_backoff_s: float = 20.0

    def __post_init__(self) -> None:
        for name in ("tenant_id", "client_id", "client_secret", "mailbox"):
            if not str(getattr(self, name)).strip():
                raise OutlookConnectorError(f"Graph setting {name} is not configured")
        if not valid_address(self.mailbox):
            raise OutlookConnectorError("the Graph mailbox must be a mailbox address (user principal name)")
        for url, hosts, what in ((self.authority, LOGIN_HOSTS, "authority"), (self.graph_base, GRAPH_HOSTS, "base URL")):
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != "https" or (parsed.hostname or "").lower() not in hosts:
                raise OutlookConnectorError(f"the Graph {what} must be https on a Microsoft host")
        if self.max_attempts < 1:
            raise OutlookConnectorError("max_attempts must be at least 1")

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> GraphSettings:
        missing = [n for n in (ENV_TENANT, ENV_CLIENT, ENV_SECRET, ENV_MAILBOX) if not environ.get(n)]
        if missing:
            raise OutlookConnectorError("Graph is not configured; missing: " + ", ".join(missing))
        extra = {}
        if environ.get(ENV_AUTHORITY):
            extra["authority"] = environ[ENV_AUTHORITY]
        if environ.get(ENV_BASE):
            extra["graph_base"] = environ[ENV_BASE]
        return cls(tenant_id=environ[ENV_TENANT], client_id=environ[ENV_CLIENT], client_secret=environ[ENV_SECRET],
                   mailbox=environ[ENV_MAILBOX], **extra)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never follow: the Authorization header must not travel on
        return None


def _urllib_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None, *, timeout: float = 30.0):
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - https host allow-listed in settings
            raw = response.read()
            return response.status, dict(response.headers), (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, dict(exc.headers), (json.loads(raw) if raw else None)
        except ValueError:
            return exc.code, dict(exc.headers), None


class _Token:
    """In-memory access token. Its repr never shows the value."""

    __slots__ = ("value", "expires_at")

    def __init__(self, value: str, expires_at: float) -> None:
        self.value, self.expires_at = value, expires_at

    def __repr__(self) -> str:
        return "<access token hidden>"


class GraphOutlookConnector:
    """Real Outlook connector over Microsoft Graph. ``outbox`` lists only what THIS instance submitted."""

    name = "graph"
    real = True

    def __init__(self, settings: GraphSettings, directory: RecipientDirectory, *, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.directory = directory
        self._transport = transport or (lambda m, u, h, b: _urllib_transport(m, u, h, b, timeout=settings.timeout_s))
        self._sleep, self._clock = sleep, clock
        self._token: _Token | None = None
        self._token_lock = threading.Lock()
        self._submitted: dict[str, OutlookMessage] = {}
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return f"<GraphOutlookConnector mailbox-configured={bool(self.settings.mailbox)}>"

    # -- authentication ------------------------------------------------------------------------------------------

    def _access_token(self, *, force: bool = False) -> str:
        with self._token_lock:
            if not force and self._token and self._clock() < self._token.expires_at - 60:
                return self._token.value
            url = f"{self.settings.authority.rstrip('/')}/{urllib.parse.quote(self.settings.tenant_id)}/oauth2/v2.0/token"
            form = urllib.parse.urlencode({
                "grant_type": "client_credentials", "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret, "scope": SCOPE}).encode("ascii")
            try:
                status, _, payload = self._transport("POST", url, {"Content-Type": "application/x-www-form-urlencoded",
                                                                   "Accept": "application/json"}, form)
            except (OSError, TimeoutError) as exc:
                raise OutlookConnectorError("could not reach the Microsoft identity platform") from exc
            if status != 200 or not isinstance(payload, dict) or "access_token" not in payload:
                code = payload.get("error") if isinstance(payload, dict) else None
                log.warning("graph.token_failed", extra={"status": status, "error_code": str(code)[:40]})
                raise OutlookAuthError(f"Microsoft Entra refused the token request (HTTP {status}, {code or 'no error code'})")
            self._token = _Token(str(payload["access_token"]), self._clock() + float(payload.get("expires_in", 3600)))
            log.info("graph.token_acquired", extra={"status": status})
            return self._token.value

    def _invalidate(self) -> None:
        with self._token_lock:
            self._token = None

    # -- requests ----------------------------------------------------------------------------------------------------

    def _call(self, operation: str, method: str, path: str, *, body: Any = None,
              query: Mapping[str, str] | None = None, ok_404: bool = False) -> Any:
        url = self.settings.graph_base.rstrip("/") + path
        if query:
            url += "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        refreshed = False
        last = 0
        for attempt in range(1, self.settings.max_attempts + 1):
            headers = {"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            try:
                status, resp_headers, payload = self._transport(method, url, headers, data)
            except (OSError, TimeoutError):
                log.warning("graph.network_error", extra={"operation": operation, "attempt": attempt})
                status, resp_headers, payload = 0, {}, None
            request_id = (resp_headers or {}).get("request-id", "")[:64]
            log.info("graph.response", extra={"operation": operation, "status": status, "attempt": attempt,
                                              "request_id": request_id})
            if 200 <= status < 300:
                return payload
            last = status
            if status == 401 and not refreshed:
                refreshed = True
                self._invalidate()
                continue
            if status in (401, 403):
                raise OutlookAuthError(f"Graph refused the {operation} request (HTTP {status}); check the app's "
                                       "permissions and mailbox access policy")
            if status == 404 and ok_404:
                return None
            if status in RETRYABLE_STATUS or status == 0:
                if attempt < self.settings.max_attempts:
                    self._sleep(self._backoff(attempt, resp_headers))
                    continue
                break
            code = ((payload or {}).get("error") or {}).get("code", "") if isinstance(payload, dict) else ""
            raise OutlookApiError(f"Graph rejected the {operation} request (HTTP {status}, {str(code)[:60] or 'no code'})")
        raise OutlookApiError(f"Graph {operation} failed after {self.settings.max_attempts} attempt(s) "
                              f"(last HTTP {last or 'network error'})")

    def _backoff(self, attempt: int, headers: Mapping[str, str] | None) -> float:
        retry_after = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
        try:
            wait = float(retry_after) if retry_after is not None else 2.0 ** (attempt - 1)
        except ValueError:
            wait = 2.0 ** (attempt - 1)
        return max(0.0, min(wait, self.settings.max_backoff_s))

    # -- helpers --------------------------------------------------------------------------------------------------------

    def _mailbox_path(self) -> str:
        return "/users/" + urllib.parse.quote(self.settings.mailbox, safe="")

    def _find(self, folder: str, key: str) -> str | None:
        """The id of a message in ``folder`` tagged with ``key``, or ``None``."""
        quoted = key.replace("'", "''")
        flt = f"singleValueExtendedProperties/any(ep: ep/id eq '{KEY_PROPERTY}' and ep/value eq '{quoted}')"
        payload = self._call(f"lookup_{folder}", "GET", f"{self._mailbox_path()}/mailFolders/{folder}/messages",
                             query={"$filter": flt, "$select": "id", "$top": "1"}, ok_404=True)
        items = (payload or {}).get("value") or []
        return str(items[0]["id"]) if items else None

    def _recipients(self, message: OutlookMessage) -> list[dict[str, Any]]:
        addresses = self.directory.resolve(message.client_id, message.recipient_role, message.recipient_team,
                                           message.role_shared)
        if not addresses:
            raise OutlookConnectorError("no approved recipient is configured for this role; nothing was sent")
        return [{"emailAddress": {"address": a}} for a in addresses]

    def _body(self, message: OutlookMessage) -> dict[str, Any]:
        text = message.body_en
        if message.body_ar:
            text = f"{text}\n\n----\n{message.subject_ar}\n{message.body_ar}"
        return {"contentType": "Text", "content": text}

    def _create(self, message: OutlookMessage, recipients: list[dict[str, Any]]) -> str:
        created = self._call("create_draft", "POST", f"{self._mailbox_path()}/messages", body={
            "subject": message.subject_en, "body": self._body(message), "toRecipients": recipients,
            "singleValueExtendedProperties": [{"id": KEY_PROPERTY, "value": message.idempotency_key}]})
        if not isinstance(created, dict) or not created.get("id"):
            raise OutlookApiError("Graph accepted the draft but returned no message id")
        return str(created["id"])

    # -- the OutlookConnector port ---------------------------------------------------------------------------------------

    def create_draft(self, message: OutlookMessage) -> DeliveryReceipt:
        """Save as a draft in the HR mailbox. Idempotent: an existing draft or sent message with the key is reused."""
        recipients = self._recipients(message)  # fail closed, locally, before any network call
        with self._lock:
            key = message.idempotency_key
            if self._find("sentitems", key):
                return DeliveryReceipt(key, DeliveryStatus.DUPLICATE_IGNORED, detail="this message was already sent")
            existing = self._find("drafts", key)
            if existing:
                return DeliveryReceipt(key, DeliveryStatus.DUPLICATE_IGNORED, message_id=existing,
                                       detail="a draft with this key already exists")
            draft_id = self._create(message, recipients)
            return DeliveryReceipt(key, DeliveryStatus.DRAFT_CREATED, message_id=draft_id,
                                   detail="draft saved in the authorised mailbox; nothing was sent")

    def send(self, message: OutlookMessage) -> DeliveryReceipt:
        """Send an approved message. ``SUBMITTED`` means Graph accepted it for sending; delivery is NOT confirmed."""
        key = message.idempotency_key
        if not message.approval_id:
            return DeliveryReceipt(key, DeliveryStatus.BLOCKED_NO_APPROVAL,
                                   detail="delivery requires an approved gate decision; no approval_id was supplied")
        recipients = self._recipients(message)  # fail closed, locally, before any network call
        with self._lock:
            if key in self._submitted or self._find("sentitems", key):
                return DeliveryReceipt(key, DeliveryStatus.DUPLICATE_IGNORED, detail="this message was already sent")
            draft_id = self._find("drafts", key) or self._create(message, recipients)
            try:
                self._call("send", "POST", f"{self._mailbox_path()}/messages/{urllib.parse.quote(draft_id, safe='')}/send")
            except OutlookApiError:
                # A "not found" here can mean an earlier attempt already sent it (the draft moved to Sent Items).
                if self._find("sentitems", key):
                    self._submitted[key] = message
                    return DeliveryReceipt(key, DeliveryStatus.DUPLICATE_IGNORED, message_id=draft_id,
                                           detail="this message was already sent")
                raise
            self._submitted[key] = message
            return DeliveryReceipt(key, DeliveryStatus.SUBMITTED, message_id=draft_id,
                                   detail="accepted by Microsoft Graph for sending; delivery is not confirmed")

    def verify_connection(self) -> dict[str, bool]:
        """READ-ONLY readiness check: can UC-02 authenticate, and read the HR mailbox's Drafts folder?

        Creates nothing and sends nothing. Each failure raises a safe :class:`OutlookConnectorError` (status and Graph's
        error code only), so a misconfigured tenant, secret, permission or mailbox restriction is reported precisely.
        """
        self._access_token(force=True)
        folder = self._call("verify_mailbox", "GET", f"{self._mailbox_path()}/mailFolders/drafts", query={"$select": "id"})
        return {"token_acquired": True, "mailbox_readable": isinstance(folder, dict) and bool(folder.get("id"))}

    def outbox(self, client_id: str) -> list[OutlookMessage]:
        """Messages THIS instance submitted for sending. It is not a read of the mailbox."""
        return [m for _, m in sorted(self._submitted.items()) if m.client_id == client_id]


__all__ = ["ENV_AUTHORITY", "ENV_BASE", "ENV_CLIENT", "ENV_MAILBOX", "ENV_SECRET", "ENV_TENANT", "GraphOutlookConnector",
           "GraphSettings", "KEY_PROPERTY", "OutlookApiError", "OutlookAuthError"]
