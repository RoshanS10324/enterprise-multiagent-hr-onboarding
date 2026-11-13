FROM python:3.11-slim

WORKDIR /app

# Install uv
RUN pip install uv

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install production dependencies only
RUN uv sync --no-dev --frozen

# Copy source
COPY src/ ./src/
COPY brain/ ./brain/
COPY knowledge/ ./knowledge/
COPY context_layer/ ./context_layer/
COPY policy/ ./policy/

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
