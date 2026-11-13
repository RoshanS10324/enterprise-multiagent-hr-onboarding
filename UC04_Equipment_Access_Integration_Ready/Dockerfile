# UC-04 local development API (mock adapters, no authentication). NOT a production image.
# Build from the repository root:
#   docker build -t dact-uc04-equipment-access .
# Run it bound to this machine only (the API has no authentication):
#   docker run --rm -p 127.0.0.1:8000:8000 dact-uc04-equipment-access
# Then open http://127.0.0.1:8000/docs
#
# The code locates brain/, context_layer/, data/ and policy/ relative to the source tree, so the tree is copied whole
# and put on PYTHONPATH rather than installed as a wheel.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src:/app
WORKDIR /app

# Keep in step with [project].dependencies and the "api" extra in pyproject.toml.
# TODO: pin from a lock file (none exists yet) before any shared deployment.
RUN pip install "pydantic>=2.10" "pyyaml>=6.0" "fastapi>=0.115" "uvicorn>=0.30"

COPY src /app/src
COPY brain /app/brain
COPY context_layer /app/context_layer
COPY data /app/data
COPY policy /app/policy
COPY guardrails /app/guardrails
COPY connectors /app/connectors
COPY mcp /app/mcp

RUN useradd --create-home --uid 10001 agent
USER agent

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"
CMD ["python", "-m", "uvicorn", "sub_agent.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
