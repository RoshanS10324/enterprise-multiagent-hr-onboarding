"""Create (or rotate) the LOCAL DEVELOPMENT field-encryption key for UC-01.

    python scripts/create_dev_encryption_key.py            # first key
    python scripts/create_dev_encryption_key.py --rotate   # new key; the old one is kept for decryption

For APP_ENV=dev and synthetic data only. Production keys come from the
approved secret store, never from this script; Settings refuses a "dev-" key
id outside APP_ENV=dev.

The key is written to UC01_Employee_Record_Creation/.env (git-ignored) as
UC01_FIELD_ENCRYPTION_KEY with UC01_FIELD_ENCRYPTION_KEY_ID. It is never
printed, logged or committed: the script prints only the key id. On
--rotate the previous key moves to UC01_FIELD_ENCRYPTION_PREVIOUS_KEYS, so
every row already encrypted stays readable.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values, set_key

UC01 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(UC01 / "src"))

from sub_agent.crypto import (  # noqa: E402
    DEV_KEY_ID_PREFIX,
    ENV_FIELD_ENCRYPTION_KEY,
    ENV_FIELD_ENCRYPTION_KEY_ID,
    ENV_FIELD_ENCRYPTION_PREVIOUS_KEYS,
    generate_key_b64,
    load_key,
    parse_previous_keys,
)


def create(env_file: Path, *, rotate: bool, previous_id: str) -> str:
    env = {k: (v or "") for k, v in dotenv_values(env_file).items()} if env_file.exists() else {}
    app_env = env.get("APP_ENV", "dev").strip() or "dev"
    if app_env != "dev":
        raise SystemExit(f"refused: APP_ENV is '{app_env}'. Development keys are for APP_ENV=dev only.")
    current = env.get(ENV_FIELD_ENCRYPTION_KEY, "").strip()
    previous = env.get(ENV_FIELD_ENCRYPTION_PREVIOUS_KEYS, "").strip()
    if current and not rotate:
        raise SystemExit(f"refused: {ENV_FIELD_ENCRYPTION_KEY} is already set in {env_file.name}. "
                         "Use --rotate to replace it (the old key is kept for decryption).")
    if current:
        load_key(current)  # never retire an invalid key silently
        old_id = env.get(ENV_FIELD_ENCRYPTION_KEY_ID, "").strip() or previous_id
        known = parse_previous_keys(previous)
        if old_id in known:
            raise SystemExit(f"refused: key id {old_id!r} is already in {ENV_FIELD_ENCRYPTION_PREVIOUS_KEYS}")
        previous = ",".join(p for p in (previous, f"{old_id}:{current}") if p)
    key_id = f"{DEV_KEY_ID_PREFIX}{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(3)}"
    env_file.touch(exist_ok=True)
    set_key(str(env_file), ENV_FIELD_ENCRYPTION_KEY, generate_key_b64(), quote_mode="never")
    set_key(str(env_file), ENV_FIELD_ENCRYPTION_KEY_ID, key_id, quote_mode="never")
    if previous:
        set_key(str(env_file), ENV_FIELD_ENCRYPTION_PREVIOUS_KEYS, previous, quote_mode="never")
    return key_id


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rotate", action="store_true")
    parser.add_argument("--env-file", type=Path, default=UC01 / ".env")
    parser.add_argument("--previous-id", default="unversioned",
                        help="id to file the current key under when it has none (on --rotate)")
    args = parser.parse_args(argv)
    key_id = create(args.env_file, rotate=args.rotate, previous_id=args.previous_id)
    print(f"development key {key_id} written to {args.env_file.name} (value not shown)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
