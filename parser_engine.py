import os
from dotenv import load_dotenv
import tree_sitter_go as tsgo
from tree_sitter import Language, Parser, Query, QueryCursor
from neo4j import GraphDatabase

class Neo4jGraphRAGEngine:
    def __init__(self, uri, user, password):
        self.go_lang = Language(tsgo.language())
        self.parser = Parser()
        self.parser.language = self.go_lang
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self):
        self.driver.close()
    
    def retrieve_context(self, target_function, hops=1):
        """Pulls structural context for an LLM prompt from Neo4j."""
        query = """
        MATCH (target:Function {name: $name})-[relationship:CALLS*0..2]->(related:Function)
        RETURN DISTINCT related.name AS function_name
        """
        
        context_nodes = []
        with self.driver.session() as session:
            result = session.run(query, name=target_function)
            for record in result:
                context_nodes.append(record["function_name"])
                
        return context_nodes

    def ingest_directory(self, directory_path):
        """Scans a directory for Go files and ingests them into Neo4j."""
        print(f"Scanning directory: {directory_path}")
        go_files = []
        
        # 1. Walk the directory tree to find all .go files
        for root, _, files in os.walk(directory_path):
            for file in files:
                if file.endswith(".go"):
                    go_files.append(os.path.join(root, file))
                    
        print(f"Found {len(go_files)} Go files. Starting batch ingestion...\n")
        
        # 2. Process each file
        for filepath in go_files:
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    code_string = f.read()
                self._build_graph_from_code(code_string)
                print(f"  [+] Ingested: {os.path.basename(filepath)}")
            except Exception as e:
                print(f"  [!] Failed to parse {os.path.basename(filepath)}: {e}")
                
        print("\nBatch ingestion complete!")

    def _build_graph_from_code(self, code_string):
        """Internal method to map a single file's AST."""
        tree = self.parser.parse(bytes(code_string, "utf8"))
        root_node = tree.root_node

        func_query = Query(self.go_lang, "(function_declaration name: (identifier) @func.name)")
        cursor = QueryCursor(func_query)
        
        with self.driver.session() as session:
            for match in cursor.captures(root_node).get("func.name", []):
                caller_name = match.text.decode('utf8')
                
                session.run("MERGE (f:Function {name: $name})", name=caller_name)
                
                call_query = Query(self.go_lang, """
                    (call_expression function: (identifier) @call.name)
                    (call_expression function: (selector_expression) @call.name)
                """)
                call_cursor = QueryCursor(call_query)
                
                for call_match in call_cursor.captures(match.parent).get("call.name", []):
                    callee_name = call_match.text.decode('utf8')
                    
                    session.run("""
                        MERGE (caller:Function {name: $caller_name})
                        MERGE (callee:Function {name: $callee_name})
                        MERGE (caller)-[:CALLS]->(callee)
                    """, caller_name=caller_name, callee_name=callee_name)

if __name__ == "__main__":
    # Load the secrets from the .env file
    load_dotenv()
    
    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    target_directory = os.getenv("TARGET_GO_PROJECT")
    
    # Initialize the engine using the hidden variables
    engine = Neo4jGraphRAGEngine(uri, user, password)
    engine.ingest_directory(target_directory)
    engine.close()