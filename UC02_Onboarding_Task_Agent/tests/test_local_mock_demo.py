"""The isolated local demo (demo/uc02_local_mock_demo.py) runs the whole workflow with nothing real: it must pass its own checks, ignore the
caller's real Supabase/secret variables, and print no secret."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo" / "uc02_local_mock_demo.py"


class LocalMockDemoTests(unittest.TestCase):
    def run_demo(self, extra: dict[str, str]) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("UC02_", "SUPABASE_", "NEXT_PUBLIC_SUPABASE"))}
        env.update(extra)
        return subprocess.run([sys.executable, str(DEMO)], capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=300)

    def test_every_check_passes_and_the_run_is_labelled_mock(self) -> None:
        result = self.run_demo({})
        self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-1500:])
        self.assertNotIn("[FAIL]", result.stdout)
        self.assertIn("checks passed", result.stdout)
        self.assertIn("MockOutlookConnector", result.stdout)
        self.assertIn("real: False", result.stdout)
        self.assertIn("NO scheduler", result.stdout)
        self.assertIn("Nothing real was contacted, written or sent.", result.stdout)

    def test_the_callers_real_configuration_is_ignored_and_no_secret_is_printed(self) -> None:
        canary = {"SUPABASE_URL": "https://real-project-canary.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "real-key-canary",
                  "UC02_STORE_MODE": "mode-canary-sqlite", "UC02_ENV": "env-canary-production", "UC02_OUTLOOK_CONNECTOR": "graph",
                  "UC02_GRAPH_CLIENT_SECRET": "graph-secret-canary", "UC02_EVENT_SECRET": "event-secret-canary"}
        result = self.run_demo(canary)
        self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-1500:])
        for value in canary.values():
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_the_demo_contains_no_business_logic_and_blocks_the_network(self) -> None:
        text = DEMO.read_text(encoding="utf-8")
        self.assertIn("socket.create_connection = _blocked", text)
        self.assertIn("UC02_DISABLE_DOTENV", text)
        for banned in ("load_environment(", "InMemoryTableClient", "SqliteTableClient"):
            self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()
