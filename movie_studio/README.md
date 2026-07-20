# Movie Studio — 3D chat UI for the movie-director agent

A single-user, cinematic **glassmorphism** web UI that hosts the `movie_director` ADK agent and
renders every generated **image / video / audio** inline in the chat, with per-asset **download**
and a gallery **export all**. It fills the gap left by `adk web`, which shows `movie://` links as
plain text and never renders the media.

```
Browser (3D SPA)  ──SSE /chat/stream──▶  FastAPI (app.py)
      ▲   ▲                                 │  InMemoryRunner(movie_director) ──MCP──▶ movie-mcp :9100
      │   └── <img>/<video>/<audio> ───────▶│  /asset proxy → movie/generated/<user>/<project>/<name>
      └────────── download ─────────────────┘
```

## Design
- **Backend** (`app.py`): reuses `movie_agent/agent.py`'s `root_agent` via `InMemoryRunner`;
  streams a turn as SSE events (`tool` / `media` / `text` / `done`); a `/asset` proxy serves the
  bytes with correct per-extension MIME (path-traversal safe). Image/music tools return
  `movie://` links; **video tools return raw paths** — both are normalized to `/asset/...`.
- **Frontend** (`static/`): dark cinematic glass UI (design via the `ui-ux-pro-max` skill —
  Inter, `#EC4899`/`#2563EB` on `#0F172A`), animated depth backdrop, 3D-tilt media cards, tool
  activity chips, SVG icons, `prefers-reduced-motion` respected. No build step.

## Run
```bash
# 1) MCP server (in one terminal)
cd movie && GOOGLE_CLOUD_PROJECT=<proj> uv run python movie_server.py --http --port 9100

# 2) Studio (in another) — serves http://localhost:8090
GOOGLE_CLOUD_PROJECT=<proj> uv run --project movie_studio python app.py
```
Env: `MCP_URL` (default `http://localhost:9100/mcp`), `PORT` (default `8090`),
`GOOGLE_CLOUD_PROJECT` (required for Vertex). `GOOGLE_GENAI_USE_VERTEXAI`/`GOOGLE_CLOUD_LOCATION`
default automatically.

Then open **http://localhost:8090** and try: *"auto mode, photorealistic, a 2-scene story about
an astronaut who misses home."* Watch the storyboards, scene videos, and score appear inline.

## Endpoints
- `GET /` — the SPA. `GET /session` — new browser session id.
- `GET /chat/stream?session=&message=` — SSE turn (agent run).
- `GET /asset/{user}/{project}/{name}[?download=1]` — media proxy (inline or attachment).
- `GET /assets/{user}/{project}` — JSON list of a project's media.
