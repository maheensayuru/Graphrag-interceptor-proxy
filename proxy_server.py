import logging
import os
import re

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from parser_engine import Neo4jGraphRAGEngine

# ── Structured logging ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("proxy_server")

# Load secrets into memory
load_dotenv()

app = FastAPI()

# ── Neo4j engine initialisation (resilient startup) ───────────────────────────
# If Neo4j is unreachable at boot time the proxy still starts and degrades
# gracefully on every request rather than refusing to launch at all.
try:
    neo4j_engine = Neo4jGraphRAGEngine(
        uri=os.getenv("NEO4J_URI"),
        user=os.getenv("NEO4J_USER"),
        password=os.getenv("NEO4J_PASSWORD"),
    )
    logger.info("Neo4j engine initialised successfully.")
except Exception as _init_err:
    neo4j_engine = None
    logger.error(
        f"Neo4j engine failed to initialise — proxy will run in passthrough mode. "
        f"Reason: {_init_err}"
    )

# The real LLM API endpoint we are proxying to (Updated for Google AI Studio)
TARGET_LLM_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"


# ── Task 2: Dynamic function-name extraction ──────────────────────────────────

# Tier-1: keyword anchor (case-insensitive) — finds the position in the text
# where the user mentions a function-describing keyword.
_KEYWORD_ANCHOR = re.compile(
    r"(?:how\s+does|explain|what\s+(?:does|is)|debug|describe|trace|analyse|analyze)\s+",
    re.IGNORECASE,
)

# Tier-2: PascalCase identifier (case-sensitive) — matches exported Go function names.
_PASCAL_PATTERN = re.compile(r"\b([A-Z][A-Za-z0-9_]+)\b")


def extract_function_name(text: str) -> str | None:
    """Extract the most likely Go function name from a natural-language prompt.

    Strategy
    --------
    1. Search for a keyword anchor (case-insensitive).  If found, scan the
       substring *after* the keyword for the first PascalCase token — this
       skips any lowercase filler words (e.g. 'the', 'a', 'my') naturally.
       e.g. "debug the StartServer function" → "StartServer"
    2. Fall back to the first PascalCase token anywhere in the full prompt.
       e.g. "ConnectDatabase seems broken" → "ConnectDatabase"
    3. Return None if nothing matches — caller skips graph augmentation.

    Args:
        text: The raw user prompt string from the intercepted IDE payload.

    Returns:
        The extracted function name, or None if no candidate is found.
    """
    if not text:
        return None

    # Tier 1 — keyword-anchored: search after the keyword for PascalCase
    anchor_match = _KEYWORD_ANCHOR.search(text)
    if anchor_match:
        # Scan only the portion of the string after the keyword + trailing space
        suffix = text[anchor_match.end():]
        pascal_match = _PASCAL_PATTERN.search(suffix)
        if pascal_match:
            return pascal_match.group(1)

    # Tier 2 — bare PascalCase token anywhere in the prompt
    pascal_match = _PASCAL_PATTERN.search(text)
    if pascal_match:
        return pascal_match.group(1)

    return None


# ── Request handler ───────────────────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def intercept_and_augment(request: Request):
    """Intercept an IDE chat completion request, inject graph context, forward."""
    payload = await request.json()

    messages = payload.get("messages", [])
    if messages:
        last_message = messages[-1].get("content", "")

        # ── Task 2: dynamic extraction ────────────────────────────────────────
        target_function = extract_function_name(last_message)

        if target_function:
            logger.info(f"Detected target function in prompt: '{target_function}'")
        else:
            logger.info("No exportable function name detected — forwarding prompt as-is.")

        # ── Task 3: resilient Neo4j context retrieval ─────────────────────────
        context_nodes = []
        if target_function and neo4j_engine is not None:
            try:
                context_nodes = neo4j_engine.retrieve_context(target_function, hops=2)
                logger.info(
                    f"Graph context retrieved — {len(context_nodes)} related node(s) found."
                )
            except Exception as neo4j_err:
                # Neo4j offline or query failure: degrade gracefully.
                # The raw user prompt is forwarded unchanged; the IDE never sees a 500.
                logger.error(
                    f"Neo4j context retrieval failed — forwarding raw prompt. "
                    f"Reason: {neo4j_err}"
                )
                context_nodes = []

        # ── Inject graph context into the prompt if available ─────────────────
        if context_nodes:
            context_string = ", ".join(context_nodes)
            augmentation = (
                f"\n\n[SYSTEM INJECTION: The target function '{target_function}' "
                f"structurally interacts with these downstream dependencies: {context_string}]"
            )
            messages[-1]["content"] += augmentation
            logger.info("Graph context injected into prompt successfully.")

    # ── Map to Gemini and Inject Real Key ─────────────────────────────────────
    # Force the model to use a free Gemini engine since Continue sends 'gpt-4'
    payload["model"] = "gemini-2.5-flash"

    # Override the fake-key from Continue with your real Google AI Studio key
    gemini_key = os.getenv("LLM_API_KEY")
    headers = {
        "Authorization": f"Bearer {gemini_key}",
        "Content-Type": "application/json",
    }

    # ── Map to Gemini and Inject Real Key ─────────────────────────────────────
    # Force the model to use a free Gemini engine
    payload["model"] = "gemini-2.5-flash"
    
    # Force streaming mode so Continue renders the text in real-time
    payload["stream"] = True

    # NEW: Strip Continue's restrictive word-count limits from the payload
    payload.pop("max_tokens", None)
    payload.pop("max_completion_tokens", None)

    gemini_key = os.getenv("LLM_API_KEY")
    headers = {
        "Authorization": f"Bearer {gemini_key}",
        "Content-Type": "application/json",
    }

    # Create a real-time streaming pipe from Gemini -> Proxy -> VS Code
    async def stream_generator():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", TARGET_LLM_URL, json=payload, headers=headers) as response:
                async for chunk in response.aiter_bytes():
                    yield chunk

    # Return the live stream back to the IDE
    return StreamingResponse(stream_generator(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting GraphRAG Interceptor Proxy on port 8001...")
    uvicorn.run(app, host="127.0.0.1", port=8001)