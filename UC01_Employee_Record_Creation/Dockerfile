FROM python:3.13-slim

# Pinned uv version, copied from its own published image (the pattern
# recommended by Astral's docs) rather than piping a curl script.
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    APP_ENV=dev

# Dependencies first, so this layer is cached across source-only changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Now the project itself: source and the policy file it reads at runtime.
# UC01Deps defaults DEFAULT_POLICY_PATH to a path relative to this
# package's own source tree (parents[2] from policy.py), which lines up
# with /app/policy only because `uv sync` installs the local project in
# editable mode by default. UC01_POLICY_PATH is set explicitly below as a
# belt-and-braces override, in case that install-mode assumption ever
# changes (see docs/known-gaps.md).
COPY src/ ./src/
COPY policy/ ./policy/
# The agent's persona and task prompts, read at runtime by llm/brain.py
# when an advisory model is wired in (none is, by default).
COPY brain/ ./brain/
RUN uv sync --frozen --no-dev

ENV UC01_POLICY_PATH=/app/policy/uc01_policy.toml \
    UC01_BRAIN_PATH=/app/brain

RUN groupadd --system uc01 && useradd --system --gid uc01 --home-dir /app uc01 \
    && chown -R uc01:uc01 /app
USER uc01

EXPOSE 8000

CMD ["/app/.venv/bin/uvicorn", "sub_agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
