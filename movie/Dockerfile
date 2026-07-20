# movie-mcp server for Cloud Run (Streamable HTTP transport).
FROM python:3.12-slim

# uv for fast, reproducible installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Deps (same set as movie/pyproject.toml). google-genai covers imagegen/videogen/musicgen.
RUN uv pip install --system "mcp[cli]>=1.2.0" "google-genai>=0.3.0" "pydantic>=2.0"

# Source modules the server imports (film_grammar, imagegen, movie_store, musicgen, videogen)
# plus the skills dir. Tests / ad-hoc drivers are excluded via .dockerignore.
COPY *.py ./
COPY skills ./skills

# Cloud Run injects $PORT and requires binding 0.0.0.0; movie_server reads it and enables HTTP.
ENV PORT=8080
EXPOSE 8080

# No key file baked in — authenticates to Vertex via the Cloud Run service account (ADC).
CMD ["python", "movie_server.py"]
