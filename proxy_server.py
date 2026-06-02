from fastapi import FastAPI, Request
import httpx
import json
import os
from dotenv import load_dotenv
from parser_engine import Neo4jGraphRAGEngine

# Load secrets into memory
load_dotenv()

app = FastAPI()

# Initialize using the environment variables
neo4j_engine = Neo4jGraphRAGEngine(
    uri=os.getenv("NEO4J_URI"),
    user=os.getenv("NEO4J_USER"),
    password=os.getenv("NEO4J_PASSWORD")
)

# The real LLM API endpoint we are proxying to
TARGET_LLM_URL = "https://api.openai.com/v1/chat/completions"

@app.post("/v1/chat/completions")
async def intercept_and_augment(request: Request):
    # 2. Catch the payload coming from the developer's IDE
    payload = await request.json()
    
    # Extract the user's actual prompt
    messages = payload.get("messages", [])
    if messages:
        last_message = messages[-1].get("content", "")
        
        # 3. Trigger GraphRAG Context Retrieval
        # (For this MVP, we simulate detecting a target function mentioned in the prompt)
        # In reality, you'd use a regex or a quick LLM pass to extract function names from the prompt
        target_function = "InitializeSystem" # Hardcoded for testing
        
        print(f"\n[PROXY] Intercepted prompt. Fetching graph context for: {target_function}")
        context_nodes = neo4j_engine.retrieve_context(target_function, hops=2)
        
        if context_nodes:
            context_string = ", ".join(context_nodes)
            augmentation = f"\n\n[SYSTEM INJECTION: The target function structurally interacts with these downstream dependencies: {context_string}]"
            
            # Inject the graph context directly into the prompt before it leaves your machine
            messages[-1]["content"] += augmentation
            print(f"[PROXY] Context injected successfully!")

    # 4. Forward the augmented payload to the real LLM API
    headers = {
        "Authorization": request.headers.get("Authorization"),
        "Content-Type": "application/json"
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(TARGET_LLM_URL, json=payload, headers=headers)
        
    # 5. Return the LLM's response back to the IDE
    return response.json()

if __name__ == "__main__":
    import uvicorn
    print("Starting GraphRAG Interceptor Proxy on port 8000...")
    uvicorn.run(app, host="127.0.0.1", port=8001)