# Enterprise HR Agent

Enterprise HR Agent is a multi-agent AI system for HR and talent-acquisition workflows, built on
LangGraph and exposed as a FastAPI service.

## Agents
- **Orchestrator** — routes cases to the right stage-agent
- **Sourcing** — finds and ranks candidates
- **Screening** — evaluates applications
- **Scheduling** — coordinates interviews
- **Offer** — drafts and tracks offer letters
- **Onboarding** — manages day-one setup tasks

## Setup

```bash
# Install uv if you don't have it
pip install uv

# Create virtual environment and install dependencies
uv sync

# Copy config template and fill in values
cp .env.example .env
```

## Running

```bash
uv run uvicorn src.main:app --reload
```

## Testing

```bash
uv run pytest tests/
```

## Evals

```bash
uv run pytest evals/
```

## Configuration
Real secrets (API keys, connection strings) are injected from Azure Key Vault at runtime.
Local development uses `.env` (never committed). See `.env.example` for required variables.
