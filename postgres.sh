#!/usr/bin/env bash
# Manage local Postgres + pgvector container for RAG

CONTAINER_NAME="postgres-rag"
DATA_DIR="$HOME/postgres-rag-data"
PORT=5432

case "$1" in
    start)
        mkdir -p "$DATA_DIR"
        if podman ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
            echo "Postgres RAG container already running."
            exit 0
        fi
        if podman ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
            podman start "$CONTAINER_NAME"
        else
            podman run -d \
                --name "$CONTAINER_NAME" \
                -e POSTGRES_USER=raguser \
                -e POSTGRES_PASSWORD=localrag \
                -e POSTGRES_DB=brainrag \
                -p "${PORT}:5432" \
                -v "$DATA_DIR:/var/lib/postgresql/data:Z" \
                docker.io/pgvector/pgvector:pg16
        fi
        echo "Postgres RAG started on port $PORT"
        ;;
    stop)
        podman stop "$CONTAINER_NAME" 2>/dev/null || true
        echo "Postgres RAG stopped."
        ;;
    status)
        if podman ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
            echo "Postgres RAG is running."
        else
            echo "Postgres RAG is not running."
        fi
        ;;
    logs)
        podman logs "$CONTAINER_NAME"
        ;;
    shell)
        podman exec -it "$CONTAINER_NAME" psql -U raguser -d brainrag
        ;;
    *)
        echo "Usage: $0 {start|stop|status|logs|shell}"
        exit 1
        ;;
esac
