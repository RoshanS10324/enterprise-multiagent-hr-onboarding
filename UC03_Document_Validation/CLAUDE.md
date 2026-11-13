# Enterprise HR Agent — AI Coding Assistant Context

## Project overview
Enterprise HR Agent is an LangGraph-based multi-agent system for HR/talent workflows. It exposes a
FastAPI service that orchestrates a set of stage-agents (sourcing, screening, scheduling,
offer, onboarding) via a central orchestrator.

## Repo layout
| Folder | Purpose |
|---|---|
| `brain/` | Instruction, persona and reasoning prompts (versioned like code) |
| `skills/` | Skill definitions — inputs, outputs, when to use |
| `mcp/` | MCP servers: HRIS, finance, Graph, IT service desk, document tools |
| `knowledge/` | Source content and Azure AI Search index definitions |
| `context_layer/` | Per-step context assembly: case state, memory, retrieved passages, history |
| `policy/` | Autonomy levels and tool permissions (config, not code) |
| `guardrails/` | Input/output checks: PII, prompt injection, content safety, bias |
| `hooks/` | Pre/post tool-call code: approval interrupts, audit records, kill-switch |
| `evals/` | Evaluation datasets and evaluators; run on every PR |
| `monitoring/` | OpenTelemetry setup, dashboards, alert rules |
| `optimization/` | Prompt and model tuning driven by eval results |
| `specs/` | Technical spec: scope, tools, knowledge, evals, policy |
| `src/` | LangGraph app: graphs, state models, FastAPI service |
| `tests/` | Unit and integration tests (pytest) |
| `docs/` | Architecture notes, ADRs, runbooks |
| `cicd/` | Pipeline definitions: test → eval → build → deploy |

## Key rules
- `brain/`, `policy/`, `skills/`, `knowledge/` changes are treated as code changes — they
  must pass evals before merge.
- Use `context_layer/` (underscore) not `context-layer/` — the hyphen form cannot be imported.
- Secrets come from Key Vault; never commit real credentials. Use `.env.example` as the template.
- Dependency management via `uv`; edit `pyproject.toml`, not `requirements.txt`.
- Virtual environment is `.venv/` — never committed.
