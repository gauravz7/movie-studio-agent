# adk web dev harness (movie_director agent) for Cloud Run.
# NOTE: build context is the REPO ROOT — the agent loads skills from movie/skills/.
# Build via cloudbuild.adkweb.yaml (-f movie_agent/Dockerfile .).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
RUN uv pip install --system "google-adk>=2.4.0" "mcp>=1.28.0"

# The agent package + the skills it loads at import (movie/skills).
COPY movie_agent ./movie_agent
COPY movie/skills ./movie/skills

# adk web discovers agent packages in the current dir; movie_agent is one. Reads $PORT, binds 0.0.0.0.
ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "adk web --host 0.0.0.0 --port $PORT --allow_origins=*"]
