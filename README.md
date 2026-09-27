# local-brain

Unified, open-source, local-first note brain with RAG memory.

Combines an Obsidian-like UI with open-source Markdown storage and a Postgres-based RAG backend. It is the shared memory layer for OpenCode and bigus_dickus.

## Stack

- **Desktop shell**: PySide6 + QWebEngineView
- **Backend**: FastAPI
- **Editor**: Milkdown WYSIWYG Markdown
- **Graph**: D3.js force-directed graph
- **Search**: Postgres hybrid search (full-text + vector similarity via pgvector)
- **Embeddings**: fastembed (default `all-MiniLM-L6-v2`, 90 MB), Ollama optional
- **Storage**: Markdown files on disk + Postgres container

## Quick start

```bash
# Start Postgres + local-brain server
~/brain/brain.sh start

# Open desktop app
~/brain/brain.sh open

# Or open in browser
~/brain/brain.sh browser
```

## Project layout

```text
~/local-brain/
├── local_brain/
│   ├── main.py          # FastAPI app
│   ├── vault.py         # Markdown file operations
│   ├── rag.py           # Postgres RAG backend
│   ├── embeddings.py    # fastembed + Ollama embedders
│   ├── chat.py          # TrueForge chat proxy
│   └── static/          # Web UI (Milkdown + chat)
├── desktop.py           # PySide6 launcher
├── run.sh               # Server launcher
├── postgres.sh          # Postgres container manager
├── start_server.py      # Detached server starter
└── requirements.txt
```

## Commands

| Command | Description |
|---|---|
| `~/brain/brain.sh start` | Start Postgres + local-brain server |
| `~/brain/brain.sh stop` | Stop server + Postgres |
| `~/brain/brain.sh status` | Show service status |
| `~/brain/brain.sh open` | Launch desktop app |
| `~/brain/brain.sh browser` | Open in browser |
| `~/brain/brain.sh reindex` | Reindex vaults into RAG memory |
| `~/brain/brain.sh logs` | Tail server log |

## Config

`~/.config/local-brain/config.yaml`

```yaml
embedding:
  provider: fastembed          # or "ollama"
  model: sentence-transformers/all-MiniLM-L6-v2
  cache_dir: ~/.cache/brain/embeddings

ollama:
  url: http://localhost:11434

trueforge:
  url: http://localhost:8790
  model: local-llama/qwen3-8b-local

chat:
  provider: auto  # auto | trueforge | ollama
  ollama_model: llama3.2:3b

models:
  - name: "cloud fallback"
    provider: openai
    base_url: "https://api.openai.com/v1"
    model: "gpt-4o-mini"
    api_key: "${OPENAI_API_KEY}"
```

## RAG API endpoints

- `GET /api/rag/stats` — memory stats
- `POST /api/rag/index` — index a chunk
- `GET /api/rag/search?q=...&k=5` — hybrid search
- `GET /api/rag/recent?session_id=...&n=10` — recent session context
- `POST /api/rag/reindex-vault` — reindex all vaults

## Chat API endpoints

- `GET /api/models` — merged model list (Ollama + config + TrueForge default)
- `GET /api/context?message=...&k=4` — retrieved chunks for a query
- `POST /api/chat` — SSE chat stream via TrueForge with RAG context

## Integration

- OpenCode chats are auto-exported to `~/brain/local-brain/pages/opencode/` by `~/local-memory/memory.sh watch`.
- bigus_dickus automatically saves turns and retrieves context from local-brain RAG.
- The chat panel inside local-brain proxies all chat through TrueForge and injects retrieved context.
