"""watcher.py — Real-time Go source watcher for GraphRAG incremental ingestion.

This standalone script monitors the directory defined by the TARGET_GO_PROJECT
environment variable.  When any .go file is created or modified it triggers
an incremental re-parse of *only that file*, updating its nodes and edges in
Neo4j without re-scanning the entire project.

Usage
-----
    python watcher.py

Dependencies
------------
    pip install watchdog python-dotenv

The script reads all connection details from the project .env file — no
secrets are ever hardcoded here.
"""

import logging
import os
import time

from dotenv import load_dotenv
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from parser_engine import Neo4jGraphRAGEngine

# ── Structured logging ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("watcher")

# ── Configuration ─────────────────────────────────────────────────────────────
load_dotenv()

_DEBOUNCE_SECONDS = 1.0  # Suppress duplicate OS events within this window


# ── Event handler ─────────────────────────────────────────────────────────────

class GoFileChangeHandler(FileSystemEventHandler):
    """Watchdog handler that incrementally re-ingests changed .go files.

    Editors typically emit 2–3 filesystem events per save (e.g. a temporary
    swap file write followed by a rename).  A simple time-based debounce dict
    keyed on the file path prevents redundant re-parses within the same
    debounce window.
    """

    def __init__(self, engine: Neo4jGraphRAGEngine, watch_root: str):
        super().__init__()
        self._engine = engine
        self._watch_root = watch_root
        # Maps absolute path → timestamp of last processed event
        self._last_processed: dict[str, float] = {}

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _is_go_file(self, path: str) -> bool:
        return path.endswith(".go")

    def _is_debounced(self, path: str) -> bool:
        """Return True if this path was processed within the debounce window."""
        last = self._last_processed.get(path, 0.0)
        return (time.monotonic() - last) < _DEBOUNCE_SECONDS

    def _process(self, path: str) -> None:
        """Read the file and trigger an incremental Neo4j update."""
        if not self._is_go_file(path):
            return
        if self._is_debounced(path):
            logger.debug(f"Debounced duplicate event for: {path}")
            return

        self._last_processed[path] = time.monotonic()

        # Compute the stable relative path (forward-slash normalised)
        relative_path = os.path.relpath(path, self._watch_root).replace("\\", "/")

        logger.info(f"Change detected — re-ingesting: {relative_path}")

        try:
            with open(path, "r", encoding="utf-8") as f:
                code_string = f.read()
            # Delegate to the single-file incremental parser (Task 1 upgrade)
            self._engine._build_graph_from_code(code_string, relative_path)
            logger.info(f"  [+] Successfully updated graph for: {relative_path}")
        except FileNotFoundError:
            # File was deleted immediately after the event (e.g. editor temp file)
            logger.warning(f"  [!] File vanished before it could be read: {relative_path}")
        except Exception as e:
            logger.error(f"  [!] Failed to re-ingest {relative_path}: {e}")

    # ── Watchdog event callbacks ──────────────────────────────────────────────

    def on_modified(self, event):
        if not event.is_directory:
            self._process(event.src_path)

    def on_created(self, event):
        if not event.is_directory:
            self._process(event.src_path)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    watch_root = os.getenv("TARGET_GO_PROJECT")
    if not watch_root:
        logger.error(
            "TARGET_GO_PROJECT is not set in the .env file.  "
            "Please define it and restart the watcher."
        )
        raise SystemExit(1)

    watch_root = os.path.abspath(watch_root)
    if not os.path.isdir(watch_root):
        logger.error(
            f"TARGET_GO_PROJECT path does not exist or is not a directory: {watch_root}"
        )
        raise SystemExit(1)

    # ── Initialise the Neo4j engine (same credentials as proxy/parser) ────────
    try:
        engine = Neo4jGraphRAGEngine(
            uri=os.getenv("NEO4J_URI"),
            user=os.getenv("NEO4J_USER"),
            password=os.getenv("NEO4J_PASSWORD"),
        )
        logger.info("Neo4j engine initialised successfully.")
    except Exception as e:
        logger.error(
            f"Failed to connect to Neo4j — watcher cannot start.  Reason: {e}"
        )
        raise SystemExit(1)

    # ── Set up the watchdog observer ──────────────────────────────────────────
    event_handler = GoFileChangeHandler(engine=engine, watch_root=watch_root)
    observer = Observer()
    observer.schedule(event_handler, path=watch_root, recursive=True)
    observer.start()

    logger.info(
        f"Watching for .go file changes in: {watch_root}  "
        f"(debounce={_DEBOUNCE_SECONDS}s)  —  Press Ctrl+C to stop."
    )

    try:
        while observer.is_alive():
            observer.join(timeout=1)
    except KeyboardInterrupt:
        logger.info("Shutdown signal received — stopping watcher...")
    finally:
        observer.stop()
        observer.join()
        engine.close()
        logger.info("Watcher shut down cleanly.")


if __name__ == "__main__":
    main()
