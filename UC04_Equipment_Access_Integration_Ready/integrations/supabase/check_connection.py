"""Read-only check that UC-04's Supabase connection works. Prints reachability and table availability only; no secrets, no row data.

    python check_connection.py
"""

import sys

import connect


def main() -> int:
    settings = connect.load_settings()
    print("settings:", {k: ("set" if v else "MISSING") for k, v in settings.items()})
    try:
        client = connect.make_client(settings)
        ready = connect.readiness(client)
    except Exception as exc:  # noqa: BLE001
        print("NOT CONNECTED:", type(exc).__name__, getattr(exc, "args", [""])[0] if exc.args else "")
        return 1
    for table, ok in ready.items():
        print(f"  {'OK     ' if ok else 'MISSING'} {table}")
    shared_ok = all(ready[t] for t in connect.SHARED_TABLES)
    state_ok = all(ready[t] for t in connect.STATE_TABLES)
    print("CONNECTED. shared tables:", "ready" if shared_ok else "INCOMPLETE", "| UC-04 state tables:",
          "ready" if state_ok else "not created yet (state_mode='memory' only)")
    return 0 if shared_ok else 2


if __name__ == "__main__":
    sys.exit(main())
