"""Configuration for local-brain."""

import os
from pathlib import Path
from typing import Any, Dict, List

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VAULT_ROOT = Path.home() / "brain" / "local-brain"
PAGES_DIR = VAULT_ROOT / "pages"
DAILY_DIR = VAULT_ROOT / "daily"
META_DIR = VAULT_ROOT / "meta"

for d in (PAGES_DIR, DAILY_DIR, META_DIR):
    d.mkdir(parents=True, exist_ok=True)

CONFIG_DIR = Path.home() / ".config" / "local-brain"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = CONFIG_DIR / "config.yaml"

DEFAULT_CONFIG = {
    "embedding": {
        "provider": "fastembed",
        "model": "sentence-transformers/all-MiniLM-L6-v2",
        "cache_dir": str(Path.home() / ".cache" / "brain" / "embeddings"),
    },
    "ollama": {
        "url": "http://localhost:11434",
    },
    "trueforge": {
        "url": "http://localhost:8790",
    },
    "chat": {
        "provider": "auto",  # auto | trueforge | ollama
        "ollama_model": "llama3.2:3b",
    },
    "models": [],
}


def _load_config() -> Dict[str, Any]:
    if not CONFIG_FILE.exists():
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f) or {}
    except Exception:
        return DEFAULT_CONFIG.copy()

    config = DEFAULT_CONFIG.copy()
    for key, value in user_config.items():
        if isinstance(value, dict) and key in config and isinstance(config[key], dict):
            config[key].update(value)
        else:
            config[key] = value
    return config


CONFIG = _load_config()


def get(key: str, default: Any = None) -> Any:
    parts = key.split(".")
    value = CONFIG
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part, default)
        else:
            return default
    return value


def models_from_config() -> List[Dict[str, Any]]:
    return CONFIG.get("models", [])


def save_default_config() -> None:
    """Write the default config if none exists."""
    if not CONFIG_FILE.exists():
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            yaml.dump(DEFAULT_CONFIG, f, default_flow_style=False)
        os.chmod(CONFIG_FILE, 0o600)


save_default_config()
