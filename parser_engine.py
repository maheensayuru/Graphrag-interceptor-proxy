import os
import logging
from dataclasses import dataclass, field
from dotenv import load_dotenv

import tree_sitter_go as tsgo
import tree_sitter_python as tspy
import tree_sitter_javascript as tsjs
import tree_sitter_java as tsjava
from tree_sitter import Language, Parser, Query, QueryCursor
from neo4j import GraphDatabase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("parser_engine")


@dataclass
class _LanguageConfig:
    """Holds the tree-sitter Language object and grammar-specific query strings
    for one programming language.

    Attributes:
        language:        The compiled tree-sitter Language for this grammar.
        func_queries:    One or more S-expression patterns that capture function
                         / method *definitions*.  Each pattern must bind the
                         capture name ``@func.name`` to the identifier node of
                         the declared function.
        call_queries:    One or more S-expression patterns that capture function
                         / method *call sites*.  Each pattern must bind the
                         capture name ``@call.name`` to the identifier (or
                         selector) node of the callee.
    """
    language: Language
    func_queries: list[str]
    call_queries: list[str]


class Neo4jGraphRAGEngine:
    """Multi-language AST parser that upserts function call graphs into Neo4j.

    Supported languages and their file extensions:
        .go   — Go
        .py   — Python
        .js   — JavaScript  (function declarations + class methods)
        .java — Java        (class methods)

    All languages share the same unified graph schema: ``Function`` nodes keyed
    on ``{name, filepath}`` connected by ``CALLS`` relationships.  Cypher
    queries are therefore identical across languages.
    """

    def __init__(self, uri, user, password):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

        # ── Language routing table ────────────────────────────────────────────
        # Maps file extension → _LanguageConfig.
        # Query strings were validated by live tree-sitter AST probes against
        # real code snippets for each grammar.
        self._lang_configs: dict[str, _LanguageConfig] = {
            ".go": _LanguageConfig(
                language=Language(tsgo.language()),
                func_queries=[
                    "(function_declaration name: (identifier) @func.name)",
                ],
                call_queries=[
                    "(call_expression function: (identifier) @call.name)",
                    "(call_expression function: (selector_expression) @call.name)",
                ],
            ),
            ".py": _LanguageConfig(
                language=Language(tspy.language()),
                func_queries=[
                    "(function_definition name: (identifier) @func.name)",
                ],
                call_queries=[
                    "(call function: (identifier) @call.name)",
                ],
            ),
            ".js": _LanguageConfig(
                language=Language(tsjs.language()),
                func_queries=[
                    # Named function declarations: function foo() {}
                    "(function_declaration name: (identifier) @func.name)",
                    # Class method definitions: class A { foo() {} }
                    "(method_definition name: (property_identifier) @func.name)",
                ],
                call_queries=[
                    "(call_expression function: (identifier) @call.name)",
                    "(call_expression function: (member_expression) @call.name)",
                ],
            ),
            ".java": _LanguageConfig(
                language=Language(tsjava.language()),
                func_queries=[
                    "(method_declaration name: (identifier) @func.name)",
                ],
                call_queries=[
                    "(method_invocation name: (identifier) @call.name)",
                ],
            ),
        }

    def close(self):
        self.driver.close()

    def retrieve_context(self, target_function, hops=1):
        """Pulls structural context for an LLM prompt from Neo4j.

        Queries all Function nodes whose name matches *target_function*,
        regardless of which file or language they originate from, then
        traverses up to 2 outgoing CALLS hops to collect related function names.
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
        """Scans a directory for all supported source files and ingests them.

        Supported extensions: .go, .py, .js, .java

        Each file is parsed independently.  The relative path from
        *directory_path* is computed and forwarded to _build_graph_from_code
        so that nodes are keyed on (name, filepath) — preventing collisions
        between identically-named functions across different files or languages.
        """
        supported_exts = set(self._lang_configs.keys())
        logger.info(
            f"Scanning directory: {directory_path}  "
            f"(supported extensions: {', '.join(sorted(supported_exts))})"
        )
        source_files = []

        # Walk the directory tree and collect all supported source files
        for root, _, files in os.walk(directory_path):
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext in supported_exts:
                    source_files.append(os.path.join(root, file))

        logger.info(f"Found {len(source_files)} supported file(s). Starting batch ingestion...")

        # Process each file, passing its relative path as a stable identifier
        for filepath in source_files:
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
        """Parse a single source file's AST and upsert nodes/edges into Neo4j.

        The correct language grammar is selected automatically from the file
        extension embedded in *filepath*.  If the extension is not in the
        routing table the file is skipped with a warning — no exception is
        raised so batch ingestion continues uninterrupted.

        Cypher queries are identical for all languages: Function nodes keyed on
        ``{name, filepath}`` linked by ``CALLS`` relationships.

        Args:
            code_string: Raw source code as a string.
            filepath:    Relative path of the source file.  Used as the second
                         dimension of the node composite key (prevents name
                         collisions across files) and to select the language.
        """
        ext = os.path.splitext(filepath)[1].lower()
        config = self._lang_configs.get(ext)

        if config is None:
            logger.warning(
                f"  [~] Skipping unsupported file extension '{ext}': {filepath}"
            )
            return

        # Instantiate a fresh Parser for this language (cheap, stateless)
        parser = Parser()
        parser.language = config.language

        tree = parser.parse(bytes(code_string, "utf8"))
        root_node = tree.root_node

        # ── Collect all function/method definitions in this file ──────────────
        func_names_and_nodes: list[tuple[str, object]] = []
        for func_query_str in config.func_queries:
            func_query = Query(config.language, func_query_str)
            cursor = QueryCursor(func_query)
            for match_node in cursor.captures(root_node).get("func.name", []):
                func_names_and_nodes.append(
                    (match_node.text.decode("utf8"), match_node.parent)
                )

        with self.driver.session() as session:
            for caller_name, func_body_node in func_names_and_nodes:

                # ── Node upsert: keyed on BOTH name AND filepath ──────────────
                session.run(
                    "MERGE (f:Function {name: $name, filepath: $filepath})",
                    name=caller_name,
                    filepath=filepath,
                )

                # ── Collect all call sites within this function's body ─────────
                for call_query_str in config.call_queries:
                    call_query = Query(config.language, call_query_str)
                    call_cursor = QueryCursor(call_query)
                    for call_match in call_cursor.captures(func_body_node).get("call.name", []):
                        callee_name = call_match.text.decode("utf8")

                        # ── Edge upsert: callee lives in the same file ────────
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