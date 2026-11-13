"""Client side of the UC01/UC04/UC05 worker protocol.

Exactly ONE persistent worker process per colliding UC -- never a pool. UC01/UC04/UC05 hold
their own business data in memory; two independent processes for the same UC would each have
their own, unsynchronized copy of that state, so there is deliberately no round-robin here (see
docs/ONBOARDING_COMMON_ARCHITECTURE.md Worker state continuity). Every request for a given UC
passes through that UC's single `threading.Lock`, which is also the entire concurrency-safety
argument: two concurrent callers simply queue, and no two colliding UCs are ever loaded into the
same process.
"""

from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

from hr_onboarding_common.orchestration.agent_contract import current_execution
from hr_onboarding_common.orchestration.contracts import Actor, WorkerRequest, WorkerResponse
from hr_onboarding_common.orchestration.workers.config import EXPECTED_ENTRY, WorkerConfig, WorkerConfigurationError

_RUNTIME_SCRIPT = Path(__file__).parent / "uc_worker_runtime.py"

DEFAULT_TIMEOUT_S = 30.0
STARTUP_TIMEOUT_S = 120.0  # first import of a UC (langgraph, pydantic models) is much slower than a call


class WorkerCrashedError(RuntimeError):
    """The worker process died or its pipe closed. Callers must treat this as an uncertain
    outcome (re-query before retrying), never as a confirmed failure -- see state.StageStatus."""


class WorkerTimeoutError(RuntimeError):
    """No response within the configured timeout. The underlying operation may or may not have
    completed -- callers must re-query, never assume success or failure."""


class UcWorker:
    """One persistent worker process for one colliding UC. Thread-safe: every `call()` is
    serialized through `self._lock`."""

    def __init__(self, config: WorkerConfig, *, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.config = config
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue[str | None] | None = None
        # stderr goes to a file, never an undrained pipe: a worker that logs more than the OS pipe
        # buffer would otherwise block forever on its own stderr write, and so would its caller.
        self._stderr = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.config.validate()
        self._stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8", prefix=f"{self.config.uc_id}-worker-")
        self._proc = subprocess.Popen(
            [str(self.config.python_executable), str(_RUNTIME_SCRIPT), "--uc", self.config.uc_id,
             "--src-dir", str(self.config.src_dir)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
            text=True, encoding="utf-8", bufsize=1,
            env=self.config.child_environment(),  # provider credentials only for workers with llm_access
        )
        # One reader thread per process: readline() has no timeout of its own, so responses are
        # handed over through a queue the caller can wait on with one.
        lines: queue.Queue[str | None] = queue.Queue()
        stdout = self._proc.stdout

        def _pump() -> None:
            for line in stdout:
                lines.put(line)
            lines.put(None)  # EOF: the process exited or closed its stdout

        threading.Thread(target=_pump, name=f"{self.config.uc_id}-worker-stdout", daemon=True).start()
        self._lines = lines
        self._verify_identity()

    def _verify_identity(self) -> None:
        response = self._raw_call({"contract_version": "1.0", "op": "__identity_check__",
                                    "run_id": f"identity-check-{uuid.uuid4().hex[:8]}",
                                    "idempotency_key": "identity-check", "actor": {"on_behalf_of": "system"},
                                    "payload": {}}, timeout_s=max(self.timeout_s, STARTUP_TIMEOUT_S))
        if not response.get("ok"):
            raise WorkerConfigurationError(
                f"{self.config.uc_id} worker: identity check failed: {response.get('error_detail')}"
            )
        result = response["result"]
        expected_module, expected_attr = EXPECTED_ENTRY[self.config.uc_id]
        if not result.get("under_configured_src_dir"):
            raise WorkerConfigurationError(
                f"{self.config.uc_id} worker: loaded entry point from {result.get('file')}, which is "
                f"NOT under the configured src_dir {self.config.src_dir} -- some other '{expected_module.split('.')[0]}' "
                f"package is shadowing it on this interpreter's path."
            )
        if expected_attr not in result.get("qualname", "") and expected_attr != result.get("qualname"):
            raise WorkerConfigurationError(
                f"{self.config.uc_id} worker: expected entry attribute '{expected_attr}', got "
                f"'{result.get('qualname')}' from module '{result.get('module')}'."
            )

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                assert self._proc.stdin is not None
                self._proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def kill(self) -> None:
        """Hard-stop the process (a simulated crash in tests; the timeout path below)."""
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait(timeout=5)

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stderr_tail(self, limit: int = 4000) -> str:
        """Diagnostics only (operators, test failure output) -- never copied into case state."""
        if self._stderr is None:
            return ""
        self._stderr.flush()
        self._stderr.seek(0)
        return self._stderr.read()[-limit:]

    # ------------------------------------------------------------------ requests
    def _raw_call(self, wire_request: dict, *, timeout_s: float | None = None) -> dict:
        assert self._proc is not None and self._proc.stdin is not None and self._lines is not None
        try:
            self._proc.stdin.write(json.dumps(wire_request) + "\n")
            self._proc.stdin.flush()
        except OSError as exc:
            raise WorkerCrashedError(f"{self.config.uc_id} worker: request pipe closed ({exc})") from exc
        try:
            line = self._lines.get(timeout=timeout_s or self.timeout_s)
        except queue.Empty:
            # The request may still complete inside the worker, and its late response would then be
            # read as the answer to the NEXT request. The process is killed so that can never
            # happen; whether the operation took effect is unknown (UNCERTAIN to every caller).
            self.kill()
            raise WorkerTimeoutError(
                f"{self.config.uc_id} worker: no response to '{wire_request.get('op')}' within "
                f"{timeout_s or self.timeout_s:.1f}s; the worker was stopped and the outcome is unknown") from None
        if line is None:
            raise WorkerCrashedError(f"{self.config.uc_id} worker: pipe closed unexpectedly (process alive: {self.is_alive})")
        return json.loads(line)

    def call(self, op: str, payload: dict, *, actor: Actor, run_id: str | None = None,
              idempotency_key: str | None = None, timeout_s: float | None = None) -> WorkerResponse:
        """Serialized through this worker's lock. Raises WorkerCrashedError if the pipe closes, and
        WorkerTimeoutError if no response arrives in time -- callers must treat both as uncertain,
        not failed (see state.StageStatus.UNCERTAIN). The ids of the stage execution in progress, if
        any (agent_contract.current_execution), travel with the request."""
        execution = current_execution.get()
        request = WorkerRequest(
            op=op, run_id=run_id or f"run-{uuid.uuid4().hex[:12]}",
            idempotency_key=idempotency_key or uuid.uuid4().hex, actor=actor, payload=payload,
            trace_id=execution.trace_id if execution else None,
            execution_id=execution.execution_id if execution else None,
        )
        with self._lock:
            if not self.is_alive:
                raise WorkerCrashedError(f"{self.config.uc_id} worker is not running")
            wire = self._raw_call(request.to_wire(), timeout_s=timeout_s)
        return WorkerResponse.from_wire(wire)


class WorkerPool:
    """Owns exactly one `UcWorker` per colliding UC. Built once per orchestrator instance."""

    def __init__(self, configs: dict[str, WorkerConfig], *, timeout_s: float = DEFAULT_TIMEOUT_S):
        """`timeout_s` is the default per-call timeout; a UC's own `WorkerConfig.timeout_s` wins."""
        self._workers: dict[str, UcWorker] = {uc_id: UcWorker(cfg, timeout_s=cfg.timeout_s or timeout_s)
                                              for uc_id, cfg in configs.items()}

    def start_all(self) -> None:
        for worker in self._workers.values():
            worker.start()

    def stop_all(self) -> None:
        for worker in self._workers.values():
            worker.stop()

    def get(self, uc_id: str) -> UcWorker:
        return self._workers[uc_id]

    def restart(self, uc_id: str) -> None:
        """Restart a single worker. Its in-memory business state is lost -- callers must
        re-query rather than retry blindly (see docs: Worker restart, durable state)."""
        self._workers[uc_id].stop()
        self._workers[uc_id].start()
