"""Read-only: dump the live Supabase schema (tables, columns, types, PKs, FKs)
from the PostgREST OpenAPI document to docs/schema/live_schema.json and stdout."""
import json, os, re, sys
from pathlib import Path
import httpx

HERE = Path(__file__).resolve().parent.parent
for p in (HERE.parent / ".env", HERE / ".env"):
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))

url, key = os.environ["SUPABASE_URL"].rstrip("/"), os.environ["SUPABASE_SERVICE_ROLE_KEY"]
schema = os.environ.get("SUPABASE_SCHEMA", "public") or "public"
r = httpx.get(f"{url}/rest/v1/", headers={"apikey": key, "Authorization": f"Bearer {key}",
              "Accept-Profile": schema}, timeout=30)
r.raise_for_status()
defs = r.json().get("definitions", {})
out = {}
for table, d in sorted(defs.items()):
    req = set(d.get("required", []))
    cols = {}
    for c, spec in d.get("properties", {}).items():
        desc = spec.get("description", "") or ""
        cols[c] = {
            "type": spec.get("format") or spec.get("type"),
            "nullable": c not in req,
            "default": spec.get("default"),
            "pk": "<pk/>" in desc,
            "fk": (re.search(r"table='([^']+)' column='([^']+)'", desc) or [None]).groups()
                  if "<fk" in desc else None,
            "enum": spec.get("enum"),
        }
    out[table] = cols
dest = HERE / "docs" / "schema" / "live_schema.json"
dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
for t, cols in out.items():
    print(f"\n{t}")
    for c, m in cols.items():
        flags = ("PK " if m["pk"] else "") + ("" if m["nullable"] else "NOT NULL ") + \
                (f"FK->{m['fk'][0]}.{m['fk'][1]} " if m["fk"] else "") + \
                (f"default={m['default']}" if m["default"] is not None else "")
        print(f"  {c:32} {str(m['type']):24} {flags.strip()}")
