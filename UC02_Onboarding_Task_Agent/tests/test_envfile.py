"""Environment loading, development-secret bootstrap and stage validation. Uses temporary folders and explicit mappings only:
no real file, secret or database is read or written."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from onboarding_task_management import envfile
from onboarding_task_management.envfile import (
    SECRET_VARIABLES,
    check_environment,
    ensure_dev_secrets,
    load_environment,
    parse_env_file,
    set_local_defaults,
)

ROOT = Path(__file__).resolve().parents[1]
SUPABASE = {"UC02_STORE_MODE": "supabase", "SUPABASE_URL": "https://example.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "k"}


class Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def write(self, name: str, text: str) -> None:
        (self.root / name).write_text(text, encoding="utf-8")


class ParserTests(Tmp):
    def test_comments_quotes_export_and_blank_lines(self) -> None:
        self.write(".env", "# c\n\nA=1\nexport B = two \nC=\"quoted value\"\nD='single'\nE=value # trailing\nbroken line\n=novalue\n")
        self.assertEqual(parse_env_file(self.root / ".env"),
                         {"A": "1", "B": "two", "C": "quoted value", "D": "single", "E": "value"})

    def test_a_bom_is_tolerated(self) -> None:
        (self.root / ".env").write_bytes(b"\xef\xbb\xbfA=1\n")
        self.assertEqual(parse_env_file(self.root / ".env"), {"A": "1"})


class LoadingTests(Tmp):
    def test_env_then_env_local_load_and_local_overrides_env(self) -> None:
        self.write(".env", "A=from-env\nB=only-env\n")
        self.write(".env.local", "A=from-local\nC=only-local\n")
        env: dict[str, str] = {}
        report = load_environment(self.root, env)
        self.assertEqual(env, {"A": "from-local", "B": "only-env", "C": "only-local"})
        self.assertEqual(report.files_loaded, [".env", ".env.local"])
        self.assertEqual(sorted(report.variables_set), ["A", "B", "C"])

    def test_the_process_environment_always_wins_and_is_never_overwritten(self) -> None:
        self.write(".env", "A=from-file\n")
        self.write(".env.local", "A=from-local\n")
        env = {"A": "explicit-override"}
        report = load_environment(self.root, env)
        self.assertEqual(env["A"], "explicit-override")
        self.assertEqual(report.skipped_already_set, ["A"])

    def test_missing_files_are_fine_and_loading_twice_changes_nothing(self) -> None:
        env: dict[str, str] = {}
        self.assertEqual(load_environment(self.root, env).files_loaded, [])
        self.write(".env", "A=1\n")
        load_environment(self.root, env)
        again = load_environment(self.root, env)
        self.assertEqual((env, again.variables_set), ({"A": "1"}, []))

    def test_the_disable_flag_makes_the_loader_a_no_op(self) -> None:
        self.write(".env", "A=1\n")
        env = {envfile.DISABLE_FLAG: "1"}
        report = load_environment(self.root, env)
        self.assertTrue(report.disabled)
        self.assertNotIn("A", env)

    def test_the_test_suite_runs_with_dotenv_disabled(self) -> None:
        self.assertEqual(os.environ.get(envfile.DISABLE_FLAG), "1")

    def test_the_report_carries_names_never_values(self) -> None:
        self.write(".env", "SECRET_THING=do-not-leak\n")
        report = load_environment(self.root, {})
        self.assertNotIn("do-not-leak", repr(report))

    def test_the_root_is_the_uc02_project_folder_independent_of_the_working_directory(self) -> None:
        self.assertEqual(envfile.PROJECT_ROOT, ROOT)
        self.assertTrue((envfile.PROJECT_ROOT / "pyproject.toml").is_file())


class ValidationTests(unittest.TestCase):
    def test_a_complete_environment_is_ready_for_every_stage(self) -> None:
        env = {**SUPABASE, **{v: "x" for v in SECRET_VARIABLES.values()}}
        for stage in envfile.STAGE_SECRETS:
            self.assertTrue(check_environment(stage, env).ok, stage)

    def test_missing_supabase_configuration_is_reported_by_name(self) -> None:
        status = check_environment("preflight", {})
        self.assertFalse(status.ok)
        text = " ".join(status.problems)
        for needle in ("UC02_STORE_MODE", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
            self.assertIn(needle, text)

    def test_memory_and_sqlite_modes_are_not_ready(self) -> None:
        for mode in ("memory", "sqlite", ""):
            self.assertFalse(check_environment("preflight", {**SUPABASE, "UC02_STORE_MODE": mode}).ok)

    def test_each_missing_secret_is_named_for_the_stages_that_need_it(self) -> None:
        full = {**SUPABASE, **{v: "x" for v in SECRET_VARIABLES.values()}}
        for channel, variable in SECRET_VARIABLES.items():
            env = {k: v for k, v in full.items() if k != variable}
            status = check_environment("full", env)
            self.assertFalse(status.ok)
            self.assertIn(variable, " ".join(status.problems))
            self.assertFalse(status.present[variable])

    def test_read_only_stages_need_no_secret_and_other_stages_need_only_their_own(self) -> None:
        for stage in ("preflight", "verify", "setup"):
            self.assertTrue(check_environment(stage, dict(SUPABASE)).ok)
        run_env = {**SUPABASE, "UC02_APPROVAL_CALLBACK_SECRET": "x", "UC02_EVENT_SECRET": "x"}
        self.assertTrue(check_environment("run", run_env).ok)
        self.assertFalse(check_environment("documents", run_env).ok)  # needs the documents secret, not uc04/uc05
        self.assertTrue(check_environment("documents", {**SUPABASE, "UC02_APPROVAL_CALLBACK_SECRET": "x",
                                                        "UC02_DOCUMENTS_SECRET": "x"}).ok)
        self.assertFalse(check_environment("full", run_env).ok)

    def test_the_status_never_contains_a_value(self) -> None:
        status = check_environment("full", {**SUPABASE, "UC02_EVENT_SECRET": "super-secret-value"})
        self.assertNotIn("super-secret-value", repr(status))


class SecretBootstrapTests(Tmp):
    DEV = {"UC02_ENV": "development"}

    def test_reporting_without_create_writes_nothing(self) -> None:
        status = ensure_dev_secrets(create=False, root=self.root, environ=dict(self.DEV))
        self.assertEqual(set(status.values()), {"missing"})
        self.assertFalse((self.root / ".env.local").exists())

    def test_create_generates_the_five_independent_secrets_into_env_local_only(self) -> None:
        env = dict(self.DEV)
        status = ensure_dev_secrets(create=True, root=self.root, environ=env)
        self.assertEqual(set(status.values()), {"created"})
        stored = parse_env_file(self.root / ".env.local")
        self.assertEqual(set(stored), set(SECRET_VARIABLES.values()))
        values = list(stored.values())
        self.assertEqual(len(set(values)), 5)  # independent
        self.assertTrue(all(len(v) >= 40 for v in values))  # 32 random bytes, url-safe
        self.assertEqual({k: env[k] for k in stored}, stored)  # the current process sees them too
        self.assertFalse((self.root / ".env").exists())

    def test_secrets_persist_and_are_reused_not_regenerated(self) -> None:
        ensure_dev_secrets(create=True, root=self.root, environ=dict(self.DEV))
        first = parse_env_file(self.root / ".env.local")
        for _ in range(3):  # a "new PowerShell session": a fresh environment that loads the file
            env = dict(self.DEV)
            load_environment(self.root, env)
            status = ensure_dev_secrets(create=True, root=self.root, environ=env)
            self.assertEqual(set(status.values()), {"present"})
        self.assertEqual(parse_env_file(self.root / ".env.local"), first)

    def test_only_missing_secrets_are_created_and_existing_ones_are_never_changed(self) -> None:
        self.write(".env.local", "UC02_EVENT_SECRET=keep-me\n")
        env = dict(self.DEV)
        load_environment(self.root, env)
        status = ensure_dev_secrets(create=True, root=self.root, environ=env)
        self.assertEqual(status["UC02_EVENT_SECRET"], "present")
        self.assertEqual(parse_env_file(self.root / ".env.local")["UC02_EVENT_SECRET"], "keep-me")
        self.assertEqual(sum(1 for s in status.values() if s == "created"), 4)

    def test_a_secret_already_in_the_process_environment_is_not_replaced(self) -> None:
        env = {**self.DEV, "UC02_UC04_STATUS_SECRET": "from-the-shell"}
        status = ensure_dev_secrets(create=True, root=self.root, environ=env)
        self.assertEqual(status["UC02_UC04_STATUS_SECRET"], "present")
        self.assertEqual(env["UC02_UC04_STATUS_SECRET"], "from-the-shell")
        self.assertNotIn("UC02_UC04_STATUS_SECRET", parse_env_file(self.root / ".env.local"))

    def test_production_and_unset_environments_never_get_generated_secrets(self) -> None:
        for env in ({"UC02_ENV": "production"}, {}):
            with self.subTest(env=env):
                with self.assertRaises(PermissionError):
                    ensure_dev_secrets(create=True, root=self.root, environ=dict(env))
                self.assertFalse((self.root / ".env.local").exists())

    def test_no_secret_value_is_ever_returned(self) -> None:
        env = dict(self.DEV)
        status = ensure_dev_secrets(create=True, root=self.root, environ=env)
        for variable in SECRET_VARIABLES.values():
            self.assertNotIn(env[variable], repr(status))

    def test_defaults_add_the_two_non_secret_settings_only_when_nothing_defines_them(self) -> None:
        env: dict[str, str] = {}
        self.assertEqual(set(set_local_defaults(root=self.root, environ=env)), {"UC02_STORE_MODE", "UC02_ENV"})
        self.assertEqual(parse_env_file(self.root / ".env.local"), {"UC02_STORE_MODE": "supabase", "UC02_ENV": "development"})
        self.assertEqual(set_local_defaults(root=self.root, environ=env), [])

    def test_an_explicit_value_elsewhere_is_respected_by_the_defaults(self) -> None:
        env = {"UC02_ENV": "production"}
        added = set_local_defaults(root=self.root, environ=env)
        self.assertEqual(added, ["UC02_STORE_MODE"])
        self.assertEqual(env["UC02_ENV"], "production")


class ScriptTests(Tmp):
    """The helper the PowerShell scripts call, run for real against a temporary configuration (no database)."""

    def run_helper(self, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
        full = {k: v for k, v in os.environ.items() if not k.startswith("UC02_") and not k.startswith("SUPABASE_")
                and not k.startswith("NEXT_PUBLIC_SUPABASE")}
        full.update(env)
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "uc02_env.py"), *args], capture_output=True, text=True,
                              env=full, timeout=60, cwd=str(self.root))

    def test_check_with_nothing_set_fails_clearly_and_prints_no_value(self) -> None:
        result = self.run_helper("check", "--stage", "run", env={envfile.DISABLE_FLAG: "1"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("NOT READY", result.stdout)
        self.assertIn("UC02_STORE_MODE", result.stdout)

    def test_check_with_a_complete_environment_is_ready_and_leaks_nothing(self) -> None:
        env = {envfile.DISABLE_FLAG: "1", **SUPABASE, **{v: "leak-canary-value" for v in SECRET_VARIABLES.values()}}
        result = self.run_helper("check", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("READY", result.stdout)
        self.assertNotIn("leak-canary-value", result.stdout + result.stderr)
        self.assertNotIn("example.supabase.co", result.stdout)  # not even the URL

    def test_the_helper_works_from_any_working_directory(self) -> None:
        result = self.run_helper("check", "--stage", "preflight", env={envfile.DISABLE_FLAG: "1"})
        self.assertIn(str(ROOT), result.stdout)  # it found the UC-02 root from its own location, not from the cwd


class PowerShellScriptTests(unittest.TestCase):
    def test_the_scripts_exist_and_contain_no_secret_generation_or_database_calls(self) -> None:
        for name in ("setup_dev_environment.ps1", "check_environment.ps1"):
            text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertIn("uc02_env.py", text)
            for banned in ("Invoke-RestMethod", "Invoke-WebRequest", "supabase_demo", "uc02_supabase_agentic_workflow", "token_urlsafe"):
                self.assertNotIn(banned, text)

    def test_the_check_script_is_read_only(self) -> None:
        self.assertNotIn("setup", (ROOT / "scripts" / "check_environment.ps1").read_text(encoding="utf-8").split("& python")[1])

    def test_env_local_and_env_are_git_ignored_and_the_example_is_not(self) -> None:
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".env\n", ignore)
        self.assertIn(".env.*", ignore)
        self.assertIn("!.env.example", ignore)

    def test_the_example_holds_placeholders_only(self) -> None:
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                if key.endswith(("SECRET", "KEY")) or "PASSWORD" in key or key.endswith("DATABASE_URL"):
                    self.assertEqual(value.strip(), "", key)


if __name__ == "__main__":
    unittest.main()
