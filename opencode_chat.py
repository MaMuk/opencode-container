#!/usr/bin/env python3
"""Small stdlib-only host launcher; dotenv values are data, never shell code."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
ENV_FILE = Path.home() / ".config/opencode-chat/.env"
DEFAULTS = {
    "LLM_PROVIDER": "featherless",
    "LLM_BASE_URL": "https://api.featherless.ai/v1",
    "LLM_API_KEY": "",
    "LLM_MODEL": "",
    "OPENCODE_CONFIG_MODE": "env",
    "OPENCODE_PACKAGES": "git,curl,jq",
}
LEGACY_KEYS = {
    "FEATHERLESS_BASE_URL": "LLM_BASE_URL",
    "FEATHERLESS_API_KEY": "LLM_API_KEY",
    "FEATHERLESS_MODEL": "LLM_MODEL",
}
HELP = """Usage: opencode-chat [wrapper options] [OpenCode arguments]

  --cwd DIR              Mount only this directory read/write at /workspace
  --packages LIST        Temporary Debian packages, comma separated (repeatable)
  --packages-file FILE   Temporary packages, one per line (repeatable)
  --self-test            Run isolation/package tests without inference credentials
  --check-config         Validate configuration without API requests or building
  --test-api             Send one chat completion, maximum 8 output tokens
  --build                Build/reuse base and persistent images
  --help                 Show this help (use -- --help for OpenCode help)
  --                     Forward all remaining arguments verbatim to OpenCode

Both --option=value and --option value are supported. Unknown arguments are
forwarded to OpenCode. Config: ~/.config/opencode-chat/.env
OPENCODE_CONFIG_MODE=env (default) generates config from LLM_* settings.
OPENCODE_CONFIG_MODE=native copies adjacent opencode.json/JSONC and optional cli.json.
Chat-only mode has no host directory mounts. Project changes persist on the host.
"""


class Error(Exception):
    pass


def packages(values):
    result = set()
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]+", item):
                raise Error("Invalid Debian package name (use lowercase letters, digits, +, . or -).")
            result.add(item)
    return sorted(result)


def package_file(path):
    try:
        lines = Path(path).read_text().splitlines()
    except (OSError, UnicodeError):
        raise Error("Cannot read package configuration file.") from None
    values = []
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line or len(packages([line])) != 1:
            raise Error(f"Package file line {number}: expected one valid package per line.")
        values.append(line)
    return values


def parse(argv):
    options = {"cwd": None, "packages": [], "mode": None, "args": []}
    iterator = iter(argv)
    for arg in iterator:
        if arg == "--":
            options["args"].extend(iterator)
            break
        name, equal, value = arg.partition("=")
        if name in ("--cwd", "--packages", "--packages-file"):
            if not equal:
                value = next(iterator, None)
            if value is None or value.startswith("--") or (not value and name != "--packages"):
                raise Error(f"{name} requires a value.")
            if name == "--cwd":
                options["cwd"] = project_path(value)
            elif name == "--packages":
                options["packages"].extend(packages([value]))
            else:
                options["packages"].extend(package_file(value))
        elif arg in ("--self-test", "--check-config", "--test-api", "--build", "--help"):
            if options["mode"]:
                raise Error("Choose only one diagnostic mode.")
            options["mode"] = arg
        else:
            options["args"].append(arg)
    options["packages"] = packages(options["packages"])
    return options


def project_path(value):
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise Error("--cwd must refer to an existing directory.") from None
    if not path.is_dir():
        raise Error("--cwd must refer to a directory.")
    if path == Path("/"):
        raise Error("Mounting the host root filesystem is forbidden.")
    # Podman's volume syntax treats colon as an option separator.
    if ":" in str(path) or "\n" in str(path):
        raise Error("Project paths containing colon or newline are unsupported.")
    return path


def load_config(path=ENV_FILE):
    if not path.is_file():
        raise Error("Configuration is missing; run ./install.sh to create ~/.config/opencode-chat/.env.")
    path.chmod(0o600)
    config = dict(DEFAULTS)
    settings = {}
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeError):
        raise Error("Cannot read configuration file.") from None
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or (key not in DEFAULTS and key not in LEGACY_KEYS):
            raise Error(f"Configuration line {number}: expected a supported NAME=value setting.")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise Error(f"Configuration line {number}: unmatched quote.")
            value = value[1:-1]
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise Error(f"Configuration line {number}: control characters are unsupported.")
        settings[key] = value
    for old, new in LEGACY_KEYS.items():
        if old in settings and new not in settings:
            config[new] = settings[old]
    config.update({key: value for key, value in settings.items() if key in DEFAULTS})
    if config["OPENCODE_CONFIG_MODE"] not in ("env", "native"):
        raise Error("OPENCODE_CONFIG_MODE must be env or native.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", config["LLM_PROVIDER"]):
        raise Error("LLM_PROVIDER must be a lowercase provider ID (letters, digits, underscore or hyphen).")
    validate_url(config["LLM_BASE_URL"])
    if any(c.isspace() or c in "{}" for c in config["LLM_MODEL"]):
        raise Error("Model ID must not contain whitespace or configuration substitutions.")
    packages([config["OPENCODE_PACKAGES"]])
    if config["OPENCODE_CONFIG_MODE"] == "native":
        config["_native_files"] = native_files(path.parent)
    return config


def native_files(directory):
    files = {}
    for stem, suffixes in (("opencode", (".json", ".jsonc")), ("cli", (".json",))):
        paths = [directory / (stem + suffix) for suffix in suffixes]
        present = [path for path in paths if path.exists()]
        if len(present) > 1:
            raise Error(f"Native mode: choose either {stem}.json or {stem}.jsonc, not both.")
        for path in present:
            try:
                content = path.read_text()
                if path.suffix == ".json":
                    if not isinstance(json.loads(content), dict):
                        raise ValueError
                if not content.strip():
                    raise ValueError
            except (OSError, UnicodeError, ValueError):
                raise Error(f"Native mode: cannot read valid configuration from {path.name}.") from None
            files[path.name] = content
    if not any(name.startswith("opencode.") for name in files):
        raise Error("Native mode requires opencode.json or opencode.jsonc alongside .env.")
    # Keep the inherited environment entry below Linux's per-string exec limit.
    if len(json.dumps(files).encode()) > 64 * 1024:
        raise Error("Native configuration exceeds the supported 64 KiB transfer limit.")
    return files


def validate_url(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (parsed.scheme in ("http", "https") and parsed.hostname and
                 not parsed.username and not parsed.password and not parsed.query and
                 not parsed.fragment and not any(c.isspace() or c in "{}" for c in url))
        parsed.port  # Reject malformed/out-of-range ports.
    except ValueError:
        valid = False
    if not valid:
        raise Error("LLM_BASE_URL must be an HTTP(S) URL without credentials, query or fragment.")


def call(args, capture=False, env=None, check=True):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, env=env)
    if check and result.returncode:
        # Never echo command arguments or environment; diagnostics may contain secrets.
        raise Error(f"{Path(args[0]).name} failed (exit {result.returncode})." +
                    (" Check rootless Podman access with 'podman info' on the host." if capture else ""))
    return result


def rootless():
    if os.geteuid() == 0:
        raise Error("Run as your ordinary host user; rootless Podman is required.")
    if not shutil.which("podman"):
        raise Error("Podman is not installed or not on PATH.")
    info = call(["podman", "info", "--format", "{{.Host.Security.Rootless}}"], capture=True)
    if info.stdout.strip() != "true":
        raise Error("Rootless Podman is required.")


def image_exists(name):
    return call(["podman", "image", "exists", name], capture=True, check=False).returncode == 0


def base_image():
    digest = hashlib.sha256()
    for name in ("Containerfile", "container-init.sh", "generate-config.py"):
        digest.update(name.encode() + b"\0" + (ROOT / name).read_bytes())
    return "localhost/opencode-chat-base:" + digest.hexdigest()[:20]


def persistent_image(base, items):
    digest = hashlib.sha256((base + "\n" + "\n".join(items)).encode()).hexdigest()[:20]
    return "localhost/opencode-chat-packages:" + digest


def ensure_image(config, quiet=False):
    base = base_image()
    if not image_exists(base):
        print("Building pinned OpenCode base image...", flush=True)
        call(["podman", "build", "--tag", base, "--file", str(ROOT / "Containerfile"), str(ROOT)])
    items = packages([config["OPENCODE_PACKAGES"]])
    image = persistent_image(base, items)
    if image_exists(image):
        if not quiet:
            print("Reusing persistent package image.", flush=True)
        return image
    print("Building persistent package image: " + (", ".join(items) or "(none)"), flush=True)
    # Every derived image starts from the clean base; removed packages cannot linger.
    with tempfile.TemporaryDirectory(prefix="opencode-chat-build-") as directory:
        file = Path(directory) / "Containerfile"
        content = "FROM " + base + "\n"
        if items:
            content += ("RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
                        "--no-install-recommends " + " ".join(items) +
                        " && rm -rf /var/lib/apt/lists/*\n")
        file.write_text(content)
        try:
            call(["podman", "build", "--pull=never", "--tag", image, "--file", str(file), directory])
        except Error:
            raise Error("Persistent package installation/build failed; check apt output above.") from None
    return image


def container_command(image, config, cwd=None, temporary=(), args=(), executable=None,
                      capture=False, name=None, offline=False):
    command = ["podman", "run", "--rm", "--userns=keep-id:uid=1000,gid=1000",
               "--user", "0:0", "--workdir", "/workspace"]
    if name:
        command += ["--name", name]
    if offline:
        command += ["--network=none"]
    if cwd:
        command += ["--volume", str(cwd) + ":/workspace:rw"]
    if not capture:
        command += ["--interactive"]
        if sys.stdin.isatty() and sys.stdout.isatty():
            command += ["--tty"]
    env = dict(os.environ)
    for key in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "OPENCODE_CONFIG_MODE"):
        env[key] = config[key]
        command += ["--env", key]  # Values inherited, key is never in argv.
    # Old native configurations may still reference the legacy credential name.
    for old, new in LEGACY_KEYS.items():
        env[old] = config[new]
        command += ["--env", old]
    env["OPENCODE_NATIVE_FILES"] = json.dumps(config.get("_native_files", {})) if config["OPENCODE_CONFIG_MODE"] == "native" else "{}"
    command += ["--env", "OPENCODE_NATIVE_FILES"]
    env["OPENCODE_TEMP_PACKAGES"] = " ".join(packages(temporary))
    command += ["--env", "OPENCODE_TEMP_PACKAGES", "--env", "TERM=" + os.environ.get("TERM", "xterm-256color")]
    command.append(image)
    if executable is not None:
        command += ["__opencode_chat_exec__", *executable]
    else:
        command += args
    return command, env


def run_container(image, config, **options):
    command, env = container_command(image, config, **options)
    return call(command, capture=options.get("capture", False), env=env, check=False)


def check_config(config):
    print("PASS configuration file exists (mode 600)")
    print("PASS configuration mode: " + config["OPENCODE_CONFIG_MODE"])
    print("PASS API base URL syntax")
    missing = False
    for key, label in (("LLM_API_KEY", "API key"), ("LLM_MODEL", "model ID")) if config["OPENCODE_CONFIG_MODE"] == "env" else ():
        present = bool(config[key])
        missing |= not present
        print(("PASS " if present else "INCOMPLETE ") + label + (" configured" if present else " missing"))
    print("PASS package names")
    rootless()
    print("PASS rootless Podman")
    image = persistent_image(base_image(), packages([config["OPENCODE_PACKAGES"]]))
    exists = image_exists(image)
    print("PASS container image exists" if exists else "INFO container image needs building")
    if config["OPENCODE_CONFIG_MODE"] == "native":
        print("PASS native configuration files found")
        if exists:
            result = run_container(image, config, args=["debug", "config"], capture=True, offline=True)
            try:
                sources = json.loads(result.stdout)
                loaded = any(source.get("type") == "document" and
                             Path(source.get("path", "")).name in config["_native_files"] and
                             isinstance(source.get("info"), dict) for source in sources)
            except (ValueError, TypeError, AttributeError):
                loaded = False
            if result.returncode or not loaded:
                raise Error("Native configuration could not be loaded offline by OpenCode 2.0.28; check its schema and container paths.")
            print("PASS native configuration loads in OpenCode 2.0.28 (network disabled)")
        else:
            print("INFO native schema validation pending: build the image, then rerun --check-config")
    if missing:
        print("Installation can work; inference configuration is incomplete.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def test_api(config):
    if not config["LLM_API_KEY"] or not config["LLM_MODEL"]:
        raise Error("API test requires both LLM_API_KEY and LLM_MODEL (also in native mode).")
    body = json.dumps({"model": config["LLM_MODEL"],
                       "messages": [{"role": "user", "content": "Reply with OK."}],
                       "max_tokens": 8, "stream": False}).encode()
    request = urllib.request.Request(config["LLM_BASE_URL"].rstrip("/") + "/chat/completions",
                                     data=body, headers={"Content-Type": "application/json",
                                     "Authorization": "Bearer " + config["LLM_API_KEY"]})
    print("Sending one minimal chat completion (maximum 8 output tokens).", flush=True)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
            data = json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 401:
            reason = "Authentication failed (HTTP 401)."
        elif exc.code == 403:
            reason = "Access denied (HTTP 403): check account plan and selected model's access/gating."
        elif exc.code == 404:
            reason = "Endpoint or model-selection error (HTTP 404): check base URL and model ID."
        elif exc.code in (400, 422):
            reason = "Model-selection/request-format error: check model ID and chat-completion support."
        elif exc.code == 429:
            reason = "Inference rejected: rate/concurrency limit or account quota (HTTP 429)."
        elif 300 <= exc.code < 400:
            reason = "Endpoint redirects are refused to protect credentials; configure the final URL."
        else:
            reason = f"Inference/server error (HTTP {exc.code})."
        raise Error(reason) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Error("Connection failed: check DNS, networking, TLS and the configured endpoint.") from None
    except (ValueError, UnicodeError):
        raise Error("Inference returned invalid JSON.") from None
    try:
        content = data["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError
    except (KeyError, IndexError, TypeError):
        raise Error("Inference response did not contain the expected chat-completion message.") from None
    print("PASS authentication, selected model and chat-completion request format")
    # Never trust the remote server to omit credentials or terminal escape sequences.
    text = content.replace(config["LLM_API_KEY"], "[redacted]")
    print("Response: " + "".join(c for c in text if c.isprintable())[:200])
    print("Tool-calling support is model-dependent; this minimal test verifies chat completions.")


def main(argv=None):
    options = parse(sys.argv[1:] if argv is None else argv)
    if options["mode"] == "--help":
        print(HELP)
        return 0
    config = load_config()
    if options["mode"] == "--check-config":
        check_config(config)
        return 0
    if options["mode"] == "--test-api":
        test_api(config)
        return 0
    rootless()
    if options["mode"] == "--self-test":
        from self_test import run
        return run(config)
    image = ensure_image(config)
    if options["mode"] == "--build":
        print("Ready: " + image)
        return 0
    if config["OPENCODE_CONFIG_MODE"] == "env" and (not config["LLM_API_KEY"] or not config["LLM_MODEL"]):
        print("Warning: LLM credentials/model incomplete; inference is unavailable.", file=sys.stderr)
    return run_container(image, config, cwd=options["cwd"], temporary=options["packages"],
                         args=options["args"]).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Error, OSError) as exc:
        print("opencode-chat: " + (str(exc) if isinstance(exc, Error) else "Local filesystem/process operation failed."), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
