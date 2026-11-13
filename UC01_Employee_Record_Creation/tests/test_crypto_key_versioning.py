"""Key versioning, rotation, wrong keys and the development-key guard."""

from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from sub_agent.api.settings import Settings
from sub_agent.crypto import FieldCipher, FieldEncryptionError, generate_key_b64, load_key, parse_previous_keys

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "create_dev_encryption_key.py"


def _key():
    return load_key(generate_key_b64())


def test_a_versioned_key_writes_v2_and_reads_it_back():
    cipher = FieldCipher(_key(), key_id="dev-20261104-abc123")
    blob = cipher.encrypt("SYNTHETIC: AED 18,000", aad="FORM-1")
    assert blob[:1] == b"\x02" and FieldCipher.key_id_of(blob) == "dev-20261104-abc123"
    assert b"18,000" not in blob
    assert cipher.decrypt(blob, aad="FORM-1") == "SYNTHETIC: AED 18,000"


def test_an_unversioned_key_still_writes_and_reads_v1():
    key = _key()
    blob = FieldCipher(key).encrypt("SYNTHETIC", aad="FORM-1")
    assert blob[:1] == b"\x01" and FieldCipher.key_id_of(blob) is None
    assert FieldCipher(key, key_id="k2").decrypt(blob, aad="FORM-1") == "SYNTHETIC"  # same key, now named


def test_rotation_keeps_every_existing_row_readable():
    old, new = _key(), _key()
    v1_row = FieldCipher(old).encrypt("v1 SYNTHETIC", aad="F1")
    v2_row = FieldCipher(old, key_id="dev-old").encrypt("v2 SYNTHETIC", aad="F2")
    rotated = FieldCipher(new, key_id="dev-new", previous={"dev-old": old})
    assert rotated.decrypt(v1_row, aad="F1") == "v1 SYNTHETIC"
    assert rotated.decrypt(v2_row, aad="F2") == "v2 SYNTHETIC"
    assert FieldCipher.key_id_of(rotated.encrypt("x", aad="F3")) == "dev-new"  # new writes use the new key


def test_a_wrong_key_fails_without_revealing_anything():
    blob = FieldCipher(_key(), key_id="dev-a").encrypt("SYNTHETIC 18,000", aad="F1")
    with pytest.raises(FieldEncryptionError, match="not configured") as unknown:
        FieldCipher(_key(), key_id="dev-b").decrypt(blob, aad="F1")
    with pytest.raises(FieldEncryptionError, match="failed authentication"):
        FieldCipher(_key(), key_id="dev-a").decrypt(blob, aad="F1")  # same id, different key
    with pytest.raises(FieldEncryptionError, match="failed authentication"):
        FieldCipher(_key()).decrypt(FieldCipher(_key()).encrypt("x", aad="F1"), aad="F1")  # v1, wrong key
    assert "18,000" not in str(unknown.value)


def test_the_key_id_header_cannot_be_swapped():
    a, b = _key(), _key()
    blob = FieldCipher(a, key_id="ka").encrypt("SYNTHETIC", aad="F1")
    forged = b"\x02" + bytes([2]) + b"kb" + blob[2 + 2:]
    with pytest.raises(FieldEncryptionError, match="failed authentication"):
        FieldCipher(b, key_id="kb", previous={"ka": a}).decrypt(forged, aad="F1")


def test_a_row_id_mismatch_fails():
    cipher = FieldCipher(_key(), key_id="k")
    with pytest.raises(FieldEncryptionError):
        cipher.decrypt(cipher.encrypt("SYNTHETIC", aad="F1"), aad="F2")


def test_previous_keys_parse_and_never_echo_the_value():
    k = generate_key_b64()
    assert set(parse_previous_keys(f"old-1:{k}, old-2:{generate_key_b64()}")) == {"old-1", "old-2"}
    with pytest.raises(FieldEncryptionError) as bad:
        parse_previous_keys("old-1:not-base64!!")
    assert "not-base64" not in str(bad.value) and "old-1" in str(bad.value)
    with pytest.raises(FieldEncryptionError):
        parse_previous_keys(k)  # no id


def _settings(**over):
    values = dict(supabase_url="https://example-project.supabase.co", supabase_service_role_key=SecretStr("k"),
                  field_encryption_key=SecretStr(generate_key_b64()))
    values.update(over)
    return Settings(**values)


def test_a_development_key_is_refused_outside_dev():
    assert _settings(app_env="dev", field_encryption_key_id="dev-20261104-abc123")
    with pytest.raises(ValidationError, match="development encryption key"):
        _settings(app_env="prod", field_encryption_key_id="dev-20261104-abc123")
    with pytest.raises(ValidationError, match="development encryption key"):
        _settings(app_env="staging", field_encryption_key_id="managed-2026-11",
                  field_encryption_previous_keys=SecretStr(f"dev-old:{generate_key_b64()}"))
    assert _settings(app_env="prod", field_encryption_key_id="managed-2026-11")


def test_a_missing_key_is_still_refused_when_supabase_is_on():
    with pytest.raises(ValidationError, match="UC01_FIELD_ENCRYPTION_KEY"):
        _settings(field_encryption_key=SecretStr(""))


# ------------------------------------------------------------------ the dev key script


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("create_dev_encryption_key", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_script_creates_a_dev_key_and_prints_only_its_id(script, tmp_path, capsys):
    from dotenv import dotenv_values

    env = tmp_path / ".env"
    env.write_text("SUPABASE_URL=https://example-project.supabase.co\n", encoding="utf-8")
    assert script.main(["--env-file", str(env)]) == 0
    values = dotenv_values(env)
    assert values["SUPABASE_URL"] == "https://example-project.supabase.co"  # other lines kept
    assert len(base64.b64decode(values["UC01_FIELD_ENCRYPTION_KEY"])) == 32
    assert values["UC01_FIELD_ENCRYPTION_KEY_ID"].startswith("dev-")
    out = capsys.readouterr().out
    assert values["UC01_FIELD_ENCRYPTION_KEY"] not in out and values["UC01_FIELD_ENCRYPTION_KEY_ID"] in out


def test_the_script_never_overwrites_a_key_without_rotate(script, tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"UC01_FIELD_ENCRYPTION_KEY={generate_key_b64()}\n", encoding="utf-8")
    before = env.read_text(encoding="utf-8")
    with pytest.raises(SystemExit, match="already set"):
        script.main(["--env-file", str(env)])
    assert env.read_text(encoding="utf-8") == before


def test_rotation_by_script_keeps_the_old_key_for_decryption(script, tmp_path):
    from dotenv import dotenv_values

    env = tmp_path / ".env"
    script.main(["--env-file", str(env)])
    first = dotenv_values(env)
    old_cipher = FieldCipher.from_env(first)
    row = old_cipher.encrypt("SYNTHETIC", aad="F1")
    script.main(["--env-file", str(env), "--rotate"])
    second = dotenv_values(env)
    assert second["UC01_FIELD_ENCRYPTION_KEY_ID"] != first["UC01_FIELD_ENCRYPTION_KEY_ID"]
    assert FieldCipher.from_env(second).decrypt(row, aad="F1") == "SYNTHETIC"


def test_the_script_refuses_outside_dev(script, tmp_path):
    env = tmp_path / ".env"
    env.write_text("APP_ENV=prod\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="APP_ENV"):
        script.main(["--env-file", str(env)])
    assert "UC01_FIELD_ENCRYPTION_KEY" not in env.read_text(encoding="utf-8")
