* This project is a local Python reverse proxy for GraphRAG.
* We use Tree-sitter for AST parsing and Neo4j for the graph database.
* All API keys and database credentials must be loaded via the `os` or `python-dotenv` module. Never hardcode secrets.
* Keep the architecture modular: the parser engine and the FastAPI proxy should remain loosely coupled.