"""
echo_agent.py — Echo agent built on the A2A base.

Agent Card: publishes one skill "echo" that returns the input text verbatim
as an Artifact.  Serves on 0.0.0.0:<PORT> (env var PORT, default 9100).

Run:
    PORT=9100 python echo_agent.py
"""

from __future__ import annotations

import os
import logging

import uvicorn

from agents.a2a_agent import A2AAgent, _text_part, _new_artifact

logger = logging.getLogger(__name__)


class EchoAgent(A2AAgent):
    """A minimal A2A echo agent for M0 smoke testing."""

    def __init__(self, host: str = "0.0.0.0", port: int = 9100) -> None:
        self._host = host
        self._port = port
        super().__init__()  # calls _register_skills()

    # ------------------------------------------------------------------
    # A2AAgent protocol
    # ------------------------------------------------------------------

    def _build_card(self) -> dict:
        url = f"http://{self._host}:{self._port}"
        return {
            "name": "echo-agent",
            "description": (
                "Development echo agent — returns input text verbatim as an artifact. "
                "Used as the M0 smoke-test for the Travel Guild A2A base layer."
            ),
            "url": url,
            "version": "1.0.0",
            "protocolVersion": "0.3.0",
            "preferredTransport": "JSONRPC",
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
            },
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text"],
            "skills": [
                {
                    "id": "echo",
                    "name": "Echo",
                    "description": (
                        "Returns the input text unchanged as a text artifact. "
                        "Useful for verifying the A2A client↔agent roundtrip."
                    ),
                    "tags": ["echo", "test", "smoke"],
                    "examples": [
                        "hello",
                        "6 days Bali, ~$1500, solo, beaches + culture",
                    ],
                }
            ],
        }

    def _register_skills(self) -> None:
        self.register_skill("echo", self._echo_handler)

    # ------------------------------------------------------------------
    # Skill handler
    # ------------------------------------------------------------------

    async def _echo_handler(self, message: dict, task: dict) -> dict:
        """
        Extract text from the first text Part and return it verbatim
        as a text Artifact.
        """
        parts = message.get("parts", [])
        text_content: str | None = None
        for part in parts:
            if isinstance(part, dict) and part.get("kind") == "text":
                text_content = part.get("text", "")
                break

        if text_content is None:
            # No text part found; echo the entire parts list as data
            return _new_artifact(
                name="echo",
                parts=[{"kind": "data", "data": {"original_parts": parts}}],
            )

        return _new_artifact(
            name="echo",
            parts=[_text_part(text_content)],
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    port = int(os.environ.get("PORT", 9100))
    host = os.environ.get("AGENT_BIND_HOST", "127.0.0.1")

    agent = EchoAgent(host=host, port=port)
    app = agent.build_app()

    logger.info("Echo agent starting on %s:%d", host, port)
    logger.info("Agent Card: http://%s:%d/.well-known/agent-card.json", host, port)
    logger.info("RPC endpoint: http://%s:%d/", host, port)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
