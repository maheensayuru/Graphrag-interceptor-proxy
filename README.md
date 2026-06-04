# GraphRAG Interceptor Proxy & AST-Driven Codebase Ingestion Engine

A low-latency, production-grade GraphRAG (Graph-Augmented Generation) architectural pipeline that maps abstract syntax tree (AST) codebase topologies directly into a Neo4j graph database and dynamically intercepts, augments, and streams contextual infrastructure maps into upstream LLM inference lifecycles.

The engine functions as an unthrottled, OpenAI-compatible reverse proxy sitting between an IDE assistant runtime (e.g., Continue.dev, Copilot clients, or custom developer tools) and high-performance cloud LLMs. By extracting referenced code entities from raw user prompts in real-time, the engine pulls complete 2-hop downstream functional dependencies and stitches them into the system prompt context transparently before piping raw Server-Sent Events (SSE) back to the client.

---

## 🏗️ Architectural Topology

```text
  [IDE / Client Chat]
          │
          │ (Prompt: "Explain StartServer")
          ▼
┌────────────────────────────────────────────────────────┐
│               FastAPI Interceptor Proxy                │
│                 (Port 8001 /v1/...)                    │
└─────────┬────────────────────────────────────▲─────────┘
          │                                    │
          │ 1. Extract Name ("StartServer")    │ 4. Pipe Unthrottled SSE
          │ 2. Query Downstream Topology       │    Stream (text/event-stream)
          ▼                                    │
┌──────────────────┐                           │
│  Neo4j Database  │                           │
│  (Dependency)    │                           │
└─────────┬────────┘                           │
          │ 3. Inject Context Nodes            │
          ▼                                    │
┌──────────────────────────────────────────────┴─────────┐
│         Upstream Cloud Gateway (Gemini API)            │
└────────────────────────────────────────────────────────┘
```

---

## 🔥 Enterprise Features

### 1. Multi-Language AST Parsing Engine
Native static analysis and lexing infrastructure that builds code-entity semantic maps for:
* **Go:** Extracts structural schemas, interfaces, methods, and exported routines.
* **Python:** Parses module-level functions, class declarations, and internal dependencies.
* **Java:** Resolves strict class structures, methods, and package invocations.
* **JavaScript:** Maps ES6 modules, arrow bindings, and asynchronous functions.

### 2. Event-Driven Asynchronous Code Watcher
A high-performance file monitoring agent utilizing an optimized debouncing routine (`debounce=1.0s`) to mitigate disk I/O thrashing. The watcher captures structural modifications to target source files on the fly and immediately runs differential updates to the Neo4j database graph instance without blocking active developer sessions.

### 3. Fault-Tolerant Degraded Proxy Fallback
The FastAPI reverse proxy implements a strict resilient architecture. If the Neo4j database instances degrade, disconnect, or drop offline mid-session, the engine captures the connection exception, logs the event via structured handlers, and instantly downgrades to standard pass-through API routing. The client runtime experiences zero 500-level fatal errors or workflow interruptions.

### 4. Unthrottled Streaming Generator Pipeline
Bypasses restrictive client-side word-count limits and token governors by sanitizing incoming request payloads (stripping `max_tokens` and `max_completion_tokens`). Built on top of `httpx.AsyncClient` with zero-timeout tracking (`timeout=None`), it sets up a raw asynchronous byte iterator to pipeline real-time responses with sub-millisecond network overhead.

---

## 🛠️ Project Layout

```text
GRAPHRAG POC/
│
├── proxy_server.py      # FastAPI Core, prompt parser, SSE streaming mechanism
├── watcher.py           # Asynchronous watchdog monitor for file changes
├── parser_engine.py     # Neo4j graph connection driver & Cypher contextual querying
├── .env                 # Protected environment infrastructure variables
└── requirements.txt     # Python runtime dependencies
```

---

## ⚙️ Environment Configuration

Construct a robust `.env` file within the root directory of the repository following this exact structure:

```env
# Neo4j Graph Database Authentication Credentials
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your_highly_secure_database_password

# Google AI Studio Production Endpoint Configuration
LLM_API_KEY=AIzaSyYourProductionGoogleAIStudioKeyHere
```

---

## 🚀 Execution & Production Startup

To spin up the entire local GraphRAG engineering workspace, boot up two separate persistent terminal instances:

### 1. Initialize the AST Codebase Watcher
Launch the event-driven file system monitor to scan and map codebase changes into the database:
```bash
python -u watcher.py
```

### 2. Launch the High-Performance Interceptor Proxy
Initialize the FastAPI server on port `8001` with streaming pipelines activated:
```bash
python -u proxy_server.py
```

### 3. Hook the Proxy into your Target Client Runtime
Route your IDE developer tool or application runner to intercept traffic via the local endpoint configuration:
* **API Base Endpoint:** `http://127.0.0.1:8001/v1`
* **Target Inference Model:** `gemini-2.5-flash`
* **Transmission Mode:** Streaming (`stream=True` forced automatically by proxy)

---

## 📡 API Request Interception Behavior

The proxy captures incoming JSON payloads targeting `/v1/chat/completions`. If a natural language query contains specific keyword patterns (e.g., *explain, debug, trace, analyze*) followed by a structural functional identifier, the payload is dynamically modified before leaving localhost.

### Raw Incoming Payload Structure (From Client)
```json
{
  "model": "gpt-4",
  "stream": true,
  "messages": [
    {
      "role": "user",
      "content": "Please explain how the StartServer function behaves."
    }
  ]
}
```

### Mutated Payload Structure (Forwarded Upstream to Gemini)
```json
{
  "model": "gemini-2.5-flash",
  "stream": true,
  "messages": [
    {
      "role": "user",
      "content": "Please explain how the StartServer function behaves.\n\n[SYSTEM INJECTION: The target function 'StartServer' structurally interacts with these downstream dependencies: InitializeDatabase, VerifyUserToken, SetupSocketRouter]"
    }
  ]
}
```

---

## 🛡️ License

Distributed under the MIT License. See `LICENSE` for further operational legal metrics.