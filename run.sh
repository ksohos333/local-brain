#!/usr/bin/env bash
# local-brain launcher

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"

cd "${PROJECT_DIR}" || exit 1

if [[ -d "${VENV_DIR}" ]]; then
    source "${VENV_DIR}/bin/activate"
fi

export PYTHONPATH="${PROJECT_DIR}:${PYTHONPATH}"

exec uvicorn local_brain.main:app --host 127.0.0.1 --port 9876 "$@"
