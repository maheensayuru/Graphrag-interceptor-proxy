import logging
import os
import re

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
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

# The real LLM API endpoint we are proxying to
TARGET_LLM_URL = "https://api.openai.com/v1/chat/completions"


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

    # ── Forward (augmented) payload to the real LLM API ──────────────────────
    headers = {
        "Authorization": request.headers.get("Authorization"),
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(TARGET_LLM_URL, json=payload, headers=headers)

    # Return the LLM's response back to the IDE
    return JSONResponse(content=response.json(), status_code=response.status_code)


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting GraphRAG Interceptor Proxy on port 8001...")
    uvicorn.run(app, host="127.0.0.1", port=8001)