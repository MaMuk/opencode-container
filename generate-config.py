#!/usr/bin/env python3
"""Generate configuration inside the disposable container, never on the host."""
import json
import os
from pathlib import Path


def configuration(env):
    model = env.get("LLM_MODEL", "")
    provider = env.get("LLM_PROVIDER", "featherless")
    config = {
        "$schema": "https://opencode.ai/config.json",
        "update": "disable",
        "share": "disabled",
        "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
        "experimental": {"policies": [
            {"action": "provider.use", "resource": "*", "effect": "deny"},
            {"action": "provider.use", "resource": provider, "effect": "allow"},
        ]},
        "providers": {
            provider: {
                "package": "@opencode/ai/providers/openai-compatible",
                "name": provider,
                "env": ["LLM_API_KEY"],
                "settings": {
                    "baseURL": env.get("LLM_BASE_URL", "https://api.featherless.ai/v1"),
                },
                "models": {model: {"name": model}} if model else {},
            }
        },
    }
    if model:
        config["model"] = provider + "/" + model
        config["agents"] = {"title": {"model": config["model"]}}
    return config


def write_configuration(env, directory):
    directory.mkdir(parents=True, exist_ok=True)
    if env.get("OPENCODE_CONFIG_MODE", "env") == "native":
        files = json.loads(env["OPENCODE_NATIVE_FILES"])
        allowed = {"opencode.json", "opencode.jsonc", "cli.json"}
        if not files or not set(files) <= allowed or not any(name.startswith("opencode.") for name in files):
            raise ValueError("Invalid native configuration file set.")
    else:
        files = {"opencode.json": json.dumps(configuration(env), indent=2) + "\n"}
    for name, content in files.items():
        path = directory / name
        path.write_text(content)
        path.chmod(0o600)


if __name__ == "__main__":
    try:
        write_configuration(os.environ, Path("/home/agent/.config/opencode"))
    except (KeyError, ValueError, OSError, TypeError):
        raise SystemExit("Could not initialize OpenCode configuration.") from None
