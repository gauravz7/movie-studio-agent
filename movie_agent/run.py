"""Drive the movie_director agent with a natural-language request and stream its tool calls.

Usage:
  # start the movie MCP server first:
  #   cd ../movie && GOOGLE_CLOUD_PROJECT=<proj> uv run python movie_server.py --http --port 9100
  uv run python run.py "Make a 3-scene story about a witch and her cat on a broomstick"
"""

from __future__ import annotations

import asyncio
import sys

from google.adk.runners import InMemoryRunner
from google.genai import types

from agent import root_agent

APP, USER = "movie_director", "director1"


async def main(prompt: str) -> None:
    runner = InMemoryRunner(agent=root_agent, app_name=APP)
    session = await runner.session_service.create_session(app_name=APP, user_id=USER)
    print(f">>> {prompt}\n")
    msg = types.Content(role="user", parts=[types.Part(text=prompt)])
    async for event in runner.run_async(user_id=USER, session_id=session.id, new_message=msg):
        for part in (event.content.parts if event.content else []) or []:
            if getattr(part, "function_call", None):
                fc = part.function_call
                args = {k: (str(v)[:60] + "…" if len(str(v)) > 60 else v) for k, v in dict(fc.args).items()}
                print(f"  [tool→] {fc.name}({args})")
            if getattr(part, "function_response", None):
                r = str(part.function_response.response)
                print(f"  [→resp] {r[:160]}")
            if getattr(part, "text", None):
                print(f"  [agent] {part.text.strip()[:400]}")


if __name__ == "__main__":
    prompt = sys.argv[1] if len(sys.argv) > 1 else \
        "Make a 3-scene story about a witch and her cat on a broomstick over green hills."
    asyncio.run(main(prompt))
