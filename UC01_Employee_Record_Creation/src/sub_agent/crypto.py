"""Authenticated field encryption for the one column UC-01 must never
store in clear: onboarding_forms.salary_and_terms_encrypted (bytea).

SecretStr is NOT encryption - it only keeps a value out of repr() and
model_dump() inside this process. Once the value leaves Python for the
database it has no protection at all, so it is encrypted here first.

Scheme: AES-256-GCM (authenticated encryption with associated data).
  * key    - 32 bytes, supplied as base64 via UC01_FIELD_ENCRYPTION_KEY.
             Never defaulted, never derived from a password, never read
             from a column default or a database setting.
  * nonce  - 12 fresh random bytes per encryption, stored alongside.
  * AAD    - the owning form_id. This binds a ciphertext to its row: a
             ciphertext copied into a different form's row fails to
             decrypt instead of silently decrypting to someone else's
             salary.

Stored layouts (opaque bytes, written to a bytea column):
    v1  b"\\x01" || nonce (12) || ciphertext || GCM tag (16)
        AAD = form_id. Written before keys had ids; still read.
    v2  b"\\x02" || len(key_id) (1) || key_id (ascii) || nonce (12) || ciphertext || tag (16)
        AAD = form_id || "|key_id=" || key_id, so the key id in the header
        cannot be swapped without failing authentication.

Key versioning and rotation:
  * UC01_FIELD_ENCRYPTION_KEY_ID names the current key. When it is set,
    new values are written as v2 under that id; when it is not, as v1.
  * UC01_FIELD_ENCRYPTION_PREVIOUS_KEYS ("id:base64,id:base64") holds
    retired keys, used ONLY to decrypt. Rotating = move the current key
    there and set a new current key; existing rows stay readable and are
    re-encrypted under the new key the next time they are written.
  * Key ids starting with "dev-" are local development keys for synthetic
    data (scripts/create_dev_encryption_key.py). Settings refuses them
    outside APP_ENV=dev, so a development key can never protect real data.

The key material itself never appears in a message, a log or a row.
"""

from __future__ import annotations

import base64
import os
import re
import secrets
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = b"\x01"
_VERSION_KEYED = b"\x02"
_NONCE_LEN = 12
_KEY_LEN = 32
_KEY_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")

ENV_FIELD_ENCRYPTION_KEY = "UC01_FIELD_ENCRYPTION_KEY"
ENV_FIELD_ENCRYPTION_KEY_ID = "UC01_FIELD_ENCRYPTION_KEY_ID"
ENV_FIELD_ENCRYPTION_PREVIOUS_KEYS = "UC01_FIELD_ENCRYPTION_PREVIOUS_KEYS"
DEV_KEY_ID_PREFIX = "dev-"


class FieldEncryptionError(RuntimeError):
    """Raised when a field cannot be encrypted or decrypted.

    Deliberately carries no plaintext, no ciphertext and no key material:
    the message is safe to log and to surface in an error handler.
    """


def generate_key_b64() -> str:
    """A fresh base64 key, for operators setting UC01_FIELD_ENCRYPTION_KEY.

    Not called by the application at runtime - a key that the app could
    mint for itself would not be a managed key.
    """
    return base64.b64encode(secrets.token_bytes(_KEY_LEN)).decode("ascii")


def load_key(raw: str) -> bytes:
    """Decode and validate a base64 32-byte key.

    Raises FieldEncryptionError (never echoing the value) if the key is
    absent, not base64, or the wrong length.
    """
    if not raw or not raw.strip():
        raise FieldEncryptionError(
            f"{ENV_FIELD_ENCRYPTION_KEY} is not set. Salary and terms cannot be "
            "encrypted without it. Generate one with: python -c "
            '"from sub_agent.crypto import generate_key_b64; print(generate_key_b64())"'
        )
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except Exception as exc:  # noqa: BLE001 - never surface the value
        raise FieldEncryptionError(
            f"{ENV_FIELD_ENCRYPTION_KEY} is not valid base64 (length {len(raw.strip())})"
        ) from exc
    if len(key) != _KEY_LEN:
        raise FieldEncryptionError(
            f"{ENV_FIELD_ENCRYPTION_KEY} must decode to {_KEY_LEN} bytes, got {len(key)}"
        )
    return key


def check_key_id(key_id: str) -> str:
    if not _KEY_ID.fullmatch(key_id):
        raise FieldEncryptionError(
            f"{ENV_FIELD_ENCRYPTION_KEY_ID} must be 1-64 characters of A-Z a-z 0-9 . _ -"
        )
    return key_id


def parse_previous_keys(raw: str) -> dict[str, bytes]:
    """Decode UC01_FIELD_ENCRYPTION_PREVIOUS_KEYS ("id:base64,id:base64").

    Errors name the entry position and key id, never the key material.
    """
    keys: dict[str, bytes] = {}
    for position, entry in enumerate((e.strip() for e in (raw or "").split(",")), start=1):
        if not entry:
            continue
        key_id, sep, encoded = entry.partition(":")
        if not sep:
            raise FieldEncryptionError(
                f"{ENV_FIELD_ENCRYPTION_PREVIOUS_KEYS} entry {position} must be 'key_id:base64'"
            )
        key_id = check_key_id(key_id.strip())
        try:
            keys[key_id] = load_key(encoded)
        except FieldEncryptionError as exc:
            raise FieldEncryptionError(
                f"{ENV_FIELD_ENCRYPTION_PREVIOUS_KEYS} entry {position} ({key_id}) is not a valid key"
            ) from exc
    return keys


class FieldCipher:
    """Encrypt/decrypt a single sensitive field, bound to a row id.

    ``key`` is the current key; ``key_id`` (optional) names it, which makes
    new ciphertexts v2. ``previous`` maps retired key ids to keys that are
    used only to decrypt.
    """

    def __init__(self, key: bytes, *, key_id: Optional[str] = None,
                 previous: Optional[dict[str, bytes]] = None) -> None:
        if len(key) != _KEY_LEN:
            raise FieldEncryptionError(f"key must be {_KEY_LEN} bytes, got {len(key)}")
        self._key_id = check_key_id(key_id) if key_id else None
        self._aead = AESGCM(key)
        self._by_id: dict[str, AESGCM] = {}
        for old_id, old_key in (previous or {}).items():
            if len(old_key) != _KEY_LEN:
                raise FieldEncryptionError(f"previous key {old_id} must be {_KEY_LEN} bytes")
            self._by_id[check_key_id(old_id)] = AESGCM(old_key)
        if self._key_id:
            self._by_id[self._key_id] = self._aead
        # v1 rows carry no key id: try the current key first, then the retired ones.
        self._unkeyed = [self._aead] + [a for k, a in self._by_id.items() if k != self._key_id]

    @property
    def key_id(self) -> Optional[str]:
        return self._key_id

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "FieldCipher":
        source = env if env is not None else dict(os.environ)
        return cls(load_key(source.get(ENV_FIELD_ENCRYPTION_KEY, "")),
                   key_id=(source.get(ENV_FIELD_ENCRYPTION_KEY_ID) or "").strip() or None,
                   previous=parse_previous_keys(source.get(ENV_FIELD_ENCRYPTION_PREVIOUS_KEYS, "")))

    @staticmethod
    def _keyed_aad(aad: str, key_id: str) -> bytes:
        return f"{aad}|key_id={key_id}".encode("utf-8")

    @staticmethod
    def key_id_of(blob: bytes) -> Optional[str]:
        """The key id a ciphertext was written under (None for v1). Not secret."""
        if blob[:1] == _VERSION_KEYED and len(blob) > 2:
            return blob[2 : 2 + blob[1]].decode("ascii", errors="replace")
        return None

    def encrypt(self, plaintext: str, *, aad: str) -> bytes:
        nonce = secrets.token_bytes(_NONCE_LEN)
        if self._key_id is None:
            sealed = self._aead.encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))
            return _VERSION + nonce + sealed
        kid = self._key_id.encode("ascii")
        sealed = self._aead.encrypt(nonce, plaintext.encode("utf-8"), self._keyed_aad(aad, self._key_id))
        return _VERSION_KEYED + bytes([len(kid)]) + kid + nonce + sealed

    def decrypt(self, blob: bytes, *, aad: str) -> str:
        if len(blob) < len(_VERSION) + _NONCE_LEN + 16:
            raise FieldEncryptionError("ciphertext is too short to be valid")
        version = blob[:1]
        if version == _VERSION:
            nonce, sealed = blob[1 : 1 + _NONCE_LEN], blob[1 + _NONCE_LEN :]
            candidates, associated = self._unkeyed, aad.encode("utf-8")
        elif version == _VERSION_KEYED:
            size = blob[1]
            key_id = blob[2 : 2 + size].decode("ascii", errors="replace")
            rest = blob[2 + size :]
            if len(rest) < _NONCE_LEN + 16:
                raise FieldEncryptionError("ciphertext is too short to be valid")
            aead = self._by_id.get(key_id)
            if aead is None:
                raise FieldEncryptionError(
                    f"salary_and_terms was encrypted under key id {key_id!r}, which is not "
                    f"configured (current or {ENV_FIELD_ENCRYPTION_PREVIOUS_KEYS})"
                )
            nonce, sealed = rest[:_NONCE_LEN], rest[_NONCE_LEN:]
            candidates, associated = [aead], self._keyed_aad(aad, key_id)
        else:
            raise FieldEncryptionError(f"unsupported ciphertext version {version!r}")
        for aead in candidates:
            try:
                return aead.decrypt(nonce, sealed, associated).decode("utf-8")
            except InvalidTag:
                continue
        # Wrong key, tampered bytes, or a ciphertext belonging to a
        # different row. All three are the same failure to the caller.
        raise FieldEncryptionError(
            "salary_and_terms failed authentication: wrong key, altered "
            "ciphertext, or a ciphertext belonging to a different form"
        )
