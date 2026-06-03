import os
import logging
from dotenv import load_dotenv
import tree_sitter_go as tsgo
from tree_sitter import Language, Parser, Query, QueryCursor
from neo4j import GraphDatabase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("parser_engine")


class Neo4jGraphRAGEngine:
    def __init__(self, uri, user, password):
        self.go_lang = Language(tsgo.language())
        self.parser = Parser()
        self.parser.language = self.go_lang
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self):
        self.driver.close()

    def retrieve_context(self, target_function, hops=1):
        """Pulls structural context for an LLM prompt from Neo4j.

        Queries all Function nodes whose name matches *target_function*,
        regardless of which file they originate from, then traverses up to
        2 outgoing CALLS hops to collect related function names.
        """
        query = """
        MATCH (target:Function {name: $name})-[relationship:CALLS*0..2]->(related:Function)
        RETURN DISTINCT related.name AS function_name, related.filepath AS filepath
        """

        context_nodes = []
        with self.driver.session() as session:
            result = session.run(query, name=target_function)
            for record in result:
                context_nodes.append(record["function_name"])

        return context_nodes

    def ingest_directory(self, directory_path):
        """Scans a directory for Go files and ingests them into Neo4j.

        Each file is parsed independently.  The relative path from
        *directory_path* is computed and forwarded to _build_graph_from_code
        so that nodes are keyed on (name, filepath) — preventing collisions
        between identically-named functions in different packages.
        """
        logger.info(f"Scanning directory: {directory_path}")
        go_files = []

        # 1. Walk the directory tree to find all .go files
        for root, _, files in os.walk(directory_path):
            for file in files:
                if file.endswith(".go"):
                    go_files.append(os.path.join(root, file))

        logger.info(f"Found {len(go_files)} Go file(s). Starting batch ingestion...")

        # 2. Process each file, passing its relative path as a stable identifier
        for filepath in go_files:
            relative_path = os.path.relpath(filepath, directory_path)
            # Normalise to forward slashes for cross-platform graph portability
            relative_path = relative_path.replace("\\", "/")
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    code_string = f.read()
                self._build_graph_from_code(code_string, relative_path)
                logger.info(f"  [+] Ingested: {relative_path}")
            except Exception as e:
                logger.error(f"  [!] Failed to parse {relative_path}: {e}")

        logger.info("Batch ingestion complete.")

    def _build_graph_from_code(self, code_string, filepath):
        """Parse a single Go file's AST and upsert nodes/edges into Neo4j.

        Args:
            code_string: Raw Go source code as a string.
            filepath:    Relative path of the source file (used as a
                         second dimension of the node composite key so that
                         functions with the same name in different files
                         remain distinct nodes in the graph).
        """
        tree = self.parser.parse(bytes(code_string, "utf8"))
        root_node = tree.root_node

        func_query = Query(self.go_lang, "(function_declaration name: (identifier) @func.name)")
        cursor = QueryCursor(func_query)

        with self.driver.session() as session:
            for match in cursor.captures(root_node).get("func.name", []):
                caller_name = match.text.decode("utf8")

                # ── Node upsert: keyed on BOTH name AND filepath ──────────────
                session.run(
                    "MERGE (f:Function {name: $name, filepath: $filepath})",
                    name=caller_name,
                    filepath=filepath,
                )

                call_query = Query(
                    self.go_lang,
                    """
                    (call_expression function: (identifier) @call.name)
                    (call_expression function: (selector_expression) @call.name)
                    """,
                )
                call_cursor = QueryCursor(call_query)

                for call_match in call_cursor.captures(match.parent).get("call.name", []):
                    callee_name = call_match.text.decode("utf8")

                    # ── Edge upsert: callee lives in the same file ────────────
                    session.run(
                        """
                        MERGE (caller:Function {name: $caller_name, filepath: $filepath})
                        MERGE (callee:Function {name: $callee_name, filepath: $filepath})
                        MERGE (caller)-[:CALLS]->(callee)
                        """,
                        caller_name=caller_name,
                        callee_name=callee_name,
                        filepath=filepath,
                    )


if __name__ == "__main__":
    # Load the secrets from the .env file
    load_dotenv()

    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    target_directory = os.getenv("TARGET_GO_PROJECT")

    # Initialise the engine using environment variables
    engine = Neo4jGraphRAGEngine(uri, user, password)
    engine.ingest_directory(target_directory)
    engine.close()