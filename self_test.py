"""Real rootless container tests. Never send inference requests or use host data."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

import opencode_chat as oc


def require(condition, message):
    if not condition:
        raise oc.Error(message)


def probe(image, config, script, **options):
    result = oc.run_container(image, config, executable=["python3", "-c", script],
                              capture=True, **options)
    require(result.returncode == 0, "Container probe failed; run on the host and inspect Podman/apt output.")
    return result.stdout.strip()


def inspect_session(image, config, cwd=None):
    name = "opencode-chat-test-" + uuid.uuid4().hex[:12]
    command, env = oc.container_command(image, config, cwd=cwd, capture=True,
                                       name=name, offline=True,
                                       executable=["sleep", "60"])
    process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            result = oc.call(["podman", "inspect", name], capture=True, check=False)
            if result.returncode == 0:
                data = json.loads(result.stdout)[0]
                if data["State"]["Running"]:
                    return data
            if process.poll() is not None:
                raise oc.Error("Inspection test container exited before it could be inspected.")
            time.sleep(0.1)
        raise oc.Error("Inspection test container did not become ready.")
    finally:
        oc.call(["podman", "stop", "--time", "1", name], capture=True, check=False)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)


def run(config):
    passed = 0

    def report(message):
        nonlocal passed
        passed += 1
        print(f"PASS {passed:02d} {message}", flush=True)

    # Config may have real credentials; tests always replace them with blank values.
    config = dict(oc.DEFAULTS, OPENCODE_PACKAGES=config["OPENCODE_PACKAGES"])
    try:
        oc.rootless()
        report("Podman is available and rootless")
        image = oc.ensure_image(config)
        report("base and persistent images build")
        version = oc.run_container(image, config, args=["--version"], capture=True, offline=True)
        require(version.returncode == 0 and version.stdout.strip() == "opencode v2.0.28", "OpenCode version check failed.")
        report("OpenCode 2.0.28 reports its pinned version")

        # A synthetic ID containing a slash verifies provider/model ID handling.
        synthetic = dict(config, LLM_PROVIDER="mock-compatible", LLM_MODEL="test-organization/offline-model")
        debug = oc.run_container(image, synthetic, args=["debug", "config"], capture=True, offline=True)
        require(debug.returncode == 0, "OpenCode could not load generated configuration offline.")
        resolved = next(source["info"] for source in json.loads(debug.stdout) if source["type"] == "document")
        require(resolved["model"] == {"providerID": "mock-compatible", "model": "test-organization/offline-model"}, "Provider/model ID was not preserved.")
        require(resolved["providers"]["mock-compatible"]["settings"]["baseURL"] == config["LLM_BASE_URL"], "Provider endpoint mismatch.")
        probe(image, synthetic, (oc.ROOT / "provider_probe.py").read_text(), offline=True)
        # Exercise the real host reader and container transfer, with no real credentials.
        with tempfile.TemporaryDirectory(prefix="opencode-chat-native-") as directory:
            path = Path(directory)
            env_file = path / ".env"
            env_file.write_text("OPENCODE_CONFIG_MODE=native\n")
            fixture = (oc.ROOT / "opencode.json.example").read_text()
            (path / "opencode.json").write_text(fixture)
            native = oc.load_config(env_file)
            probe(image, native, (oc.ROOT / "provider_probe.py").read_text(), offline=True)
            require((path / "opencode.json").read_text() == fixture, "Native host configuration changed.")
            (path / "opencode.json").unlink()
            (path / "opencode.jsonc").write_text("// native JSONC test\n" + fixture)
            native = oc.load_config(env_file)
            debug = oc.run_container(image, native, args=["debug", "config"], capture=True, offline=True)
            require(debug.returncode == 0, "OpenCode could not load native JSONC configuration.")
            resolved = next(source["info"] for source in json.loads(debug.stdout) if source["type"] == "document")
            require({"action": "*", "resource": "*", "effect": "allow"} in resolved["permissions"], "Native configuration settings were not preserved.")
        report("generated and native JSON/JSONC configs load; both provider modes emit correct mock requests")

        chat = inspect_session(image, native)
        require(not chat.get("Mounts"), "Chat-only container unexpectedly mounts host data.")
        require(not chat["HostConfig"].get("Privileged"), "Unexpected privileged container.")
        report("chat-only mode has no host directory bind mounts")
        with tempfile.TemporaryDirectory(prefix="opencode-chat-self-test-") as directory:
            parent = Path(directory)
            project = parent / "project with spaces"
            project.mkdir()
            sentinel = parent / "outside-sentinel"
            sentinel.write_text("host-only\n")
            (project / "input.txt").write_text("readable\n")
            value = probe(image, config,
                          "from pathlib import Path; p=Path('/workspace/input.txt'); "
                          "assert p.read_text()=='readable\\n'; p.write_text('modified\\n'); "
                          "Path('/workspace/output.txt').write_text('created\\n')", cwd=project, offline=True)
            require((project / "input.txt").read_text() == "modified\n", "Project modification did not persist.")
            require((project / "output.txt").stat().st_uid == os.getuid(), "Generated file has incorrect host ownership.")
            report("project files can be read/modified with host UID ownership")
            probe(image, config, "from pathlib import Path; assert not Path(" + repr(str(sentinel)) + ").exists()", cwd=project, offline=True)
            require(sentinel.read_text() == "host-only\n", "Outside sentinel changed.")
            report("created outside sentinel is inaccessible through its host filesystem path")
            mounted = inspect_session(image, config, cwd=project)
            require(len(mounted["Mounts"]) == 1 and mounted["Mounts"][0]["Destination"] == "/workspace" and
                    Path(mounted["Mounts"][0]["Source"]) == project and mounted["Mounts"][0]["RW"], "Project mount set differs from requested read/write directory.")
            for inspected in (chat, mounted):
                require(all(m["Source"] != str(oc.ENV_FILE) for m in inspected.get("Mounts", [])), ".env was mounted.")

        identity = probe(image, config, "import os; assert os.getuid()==1000 and os.getuid()!=0; print(os.getcwd())", offline=True)
        require(identity == "/workspace", "Working directory differs from /workspace.")
        report("OpenCode's execution user is non-root and cwd is /workspace")
        probe(image, config, "import subprocess; assert subprocess.check_output(['sh','-c','printf shell-ok'])==b'shell-ok'", offline=True)
        report("shell commands execute inside the container")

        installed = oc.packages([config["OPENCODE_PACKAGES"]])
        probe(image, config, "import subprocess; names=" + repr(installed) + "; "
              "assert all(subprocess.check_output(['dpkg-query','-W','-f=${Status}',p])==b'install ok installed' for p in names)", offline=True)
        report("configured persistent packages are installed")
        # Select a package absent from the actual persistent image, even with custom defaults.
        available = probe(image, config,
                          "import subprocess; names=['tree','sl','toilet','hello','cowsay','figlet']; "
                          "print(next((p for p in names if subprocess.run(['dpkg-query','-W','-f=${Status}',p],capture_output=True,text=True).stdout != 'install ok installed'),''))", offline=True)
        require(bool(available), "All temporary test packages already installed; choose an unused test package.")
        probe(image, config,
              "import subprocess; assert subprocess.check_output(['dpkg-query','-W','-f=${Status}'," + repr(available) + "])==b'install ok installed'", temporary=[available])
        report("temporary packages install for their session")
        probe(image, config,
              "import subprocess; assert subprocess.run(['dpkg-query','-W','-f=${Status}'," + repr(available) + "],capture_output=True).stdout!=b'install ok installed'", offline=True)
        report("temporary packages are absent in the following session")
        with tempfile.TemporaryDirectory(prefix="opencode-chat-parser-") as directory:
            file = Path(directory) / "tools.conf"
            file.write_text("# comment\n\n tree \njq\ntree\n")
            parsed = oc.parse(["--packages=jq,curl", "--packages-file", str(file)])
            require(parsed["packages"] == ["curl", "jq", "tree"], "Package-file merge/deduplication failed.")
        report("package configuration files merge and deduplicate correctly")
        try:
            oc.packages(["jq;touch /tmp/no"])
        except oc.Error as exc:
            require("Invalid Debian package" in str(exc), "Invalid name error is unhelpful.")
        else:
            raise oc.Error("Invalid package name was accepted.")
        report("invalid package names give useful errors")

        changed = dict(config, OPENCODE_PACKAGES=",".join([*installed, available]))
        changed_image = oc.ensure_image(changed)
        require(changed_image != image and oc.image_exists(changed_image), "Changed package list did not produce a new image.")
        probe(changed_image, changed,
              "import subprocess; assert subprocess.check_output(['dpkg-query','-W','-f=${Status}'," + repr(available) + "])==b'install ok installed'", offline=True)
        require(oc.ensure_image(config, quiet=True) == image, "Removing test package did not restore clean image.")
        report("persistent package changes build a new image; removal returns to the clean package set")
        before = oc.call(["podman", "image", "inspect", "--format", "{{.Id}}", changed_image], capture=True).stdout
        require(oc.ensure_image(changed, quiet=True) == changed_image, "Unchanged package list returned a different tag.")
        after = oc.call(["podman", "image", "inspect", "--format", "{{.Id}}", changed_image], capture=True).stdout
        require(before == after, "Unchanged list unexpectedly rebuilt image.")
        report("unchanged package lists reuse the same image")
        name = "opencode-chat-test-" + uuid.uuid4().hex[:12]
        probe(image, config, "print('removed')", name=name, offline=True)
        require(oc.call(["podman", "container", "exists", name], capture=True, check=False).returncode == 1, "--rm failed to remove container.")
        report("container is removed after execution")
        report("host .env is never mounted; project session has only the selected bind mount")
        print(f"\n{passed}/18 PASS. No external inference requests were sent; provider traffic used a local mock.")
        print("These checks demonstrate the tested filesystem isolation; they are not a general security guarantee.")
        return 0
    except (oc.Error, OSError, ValueError, KeyError) as exc:
        print("FAIL " + (str(exc) if isinstance(exc, oc.Error) else "Unexpected self-test result."), flush=True)
        print(f"{passed}/18 tests passed before failure; no external inference requests were sent.")
        return 1
