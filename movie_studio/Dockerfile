# movie-studio (FastAPI UI + embedded movie_director agent) for Cloud Run.
# NOTE: build context is the REPO ROOT, not movie_studio/ — the app imports movie_agent/agent.py,
# which loads skills from movie/skills/. Build via cloudbuild.studio.yaml (-f movie_studio/Dockerfile .).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Deps from movie_studio/pyproject.toml (google-adk pulls google-genai transitively).
# app.py persists sessions via DatabaseSessionService (SQLite) -> needs sqlalchemy + aiosqlite.
RUN uv pip install --system "fastapi>=0.110" "uvicorn>=0.29" "google-adk[db]>=2.4.0" \
    "mcp>=1.28.0" "sqlalchemy>=2.0" "aiosqlite>=0.19"

# App + the sibling modules it imports at runtime.
COPY movie_studio ./movie_studio
COPY movie_agent ./movie_agent
COPY movie/skills ./movie/skills

# app.py reads $PORT and binds 0.0.0.0. GEN_ROOT is /app/movie/generated — mount the shared
# media bucket there (gcsfuse) so the proxy can serve images the movie-mcp service produced.
ENV PORT=8080
EXPOSE 8080

CMD ["python", "movie_studio/app.py"]
