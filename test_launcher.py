"""Host-only regression tests; no Podman, installation or API account needed."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import opencode_chat as oc

spec = importlib.util.spec_from_file_location("generator", oc.ROOT / "generate-config.py")
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class LauncherTests(unittest.TestCase):
    def test_packages_deduplicate_and_sort(self):
        self.assertEqual(oc.packages(["nmap,jq,jq", " curl "]), ["curl", "jq", "nmap"])

    def test_reject_package_injection(self):
        for name in ["--allow-unauthenticated", "jq;id", "$(id)", "jq x", "../jq", "jq=1", "jq:amd64"]:
            with self.subTest(name=name), self.assertRaises(oc.Error):
                oc.packages([name])

    def test_package_file_one_per_line(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "packages"
            path.write_text("# comment\n\n jq \nnmap\njq\n")
            self.assertEqual(oc.parse(["--packages-file=" + str(path), "--packages", "curl,jq"])["packages"], ["curl", "jq", "nmap"])
            for invalid in ["jq,curl", "jq # comment", "--option", "$(id)"]:
                path.write_text(invalid)
                with self.assertRaises(oc.Error):
                    oc.package_file(path)

    def test_cwd_canonical_and_separator(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / "project"
            project.mkdir()
            link = Path(folder) / "link"
            link.symlink_to(project)
            for args in [["--cwd=" + str(link)], ["--cwd", str(link)]]:
                self.assertEqual(oc.parse(args)["cwd"], project.resolve())
            self.assertEqual(oc.parse(["--", "--cwd=/", "--help"])["args"], ["--cwd=/", "--help"])

    def test_bad_cwd(self):
        for value in ["/", "/no-such-opencode-chat-directory"]:
            with self.assertRaises(oc.Error):
                oc.project_path(value)

    def test_options_and_missing_values(self):
        for argv in [["--cwd"], ["--cwd="], ["--packages-file"], ["--packages", "--help"], ["--self-test", "--build"]]:
            with self.subTest(argv=argv), self.assertRaises(oc.Error):
                oc.parse(argv)
        self.assertEqual(oc.parse(["run", "a prompt", "--model", "featherless/org/name"])["args"], ["run", "a prompt", "--model", "featherless/org/name"])

    def test_dotenv_never_executes_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text('LLM_API_KEY="$(touch /tmp/never-executed-by-parser)"\nLLM_MODEL=org/model\n')
            config = oc.load_config(path)
            self.assertEqual(config["LLM_API_KEY"], "$(touch /tmp/never-executed-by-parser)")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text('LLM_API_KEY=secret\nINVALID=secret\n')
            with self.assertRaises(oc.Error) as exc:
                oc.load_config(path)
            self.assertNotIn("secret", str(exc.exception))

    def test_url_validation(self):
        for url in ["https://api.featherless.ai/v1", "http://localhost:8080/v1"]:
            oc.validate_url(url)
        for url in ["file:///etc/passwd", "https://user:secret@example.org/v1", "https://", "http://x:abc", "http://x:99999", "https://x/v1?key=secret", "https://x/{env:KEY}"]:
            with self.subTest(url=url), self.assertRaises(oc.Error):
                oc.validate_url(url)

    def test_fingerprints(self):
        base = oc.base_image()
        self.assertEqual(oc.persistent_image(base, oc.packages(["jq,curl,jq"])), oc.persistent_image(base, oc.packages(["curl,jq"])))
        self.assertNotEqual(oc.persistent_image(base, ["jq"]), oc.persistent_image(base, []))
        self.assertNotEqual(oc.persistent_image(base, []), oc.persistent_image(base + "changed", []))

    def test_configuration_preserves_model_and_no_secret(self):
        config = generator.configuration(dict(oc.DEFAULTS, LLM_MODEL="organization/model", LLM_API_KEY="private-secret"))
        self.assertEqual(config["model"], "featherless/organization/model")
        self.assertIn("organization/model", config["providers"]["featherless"]["models"])
        self.assertEqual(config["agents"]["title"]["model"], config["model"])
        self.assertNotIn("private-secret", json.dumps(config))
        self.assertNotIn("model", generator.configuration(oc.DEFAULTS))

    def test_custom_provider_generation(self):
        config = generator.configuration(dict(oc.DEFAULTS, LLM_PROVIDER="another-api", LLM_BASE_URL="https://example.org/v1", LLM_MODEL="org/model"))
        self.assertEqual(config["experimental"]["policies"][-1], {"action": "provider.use", "resource": "another-api", "effect": "allow"})
        self.assertEqual(config["model"], "another-api/org/model")
        self.assertEqual(config["providers"]["another-api"]["settings"], {"baseURL": "https://example.org/v1"})
        self.assertEqual(config["providers"]["another-api"]["env"], ["LLM_API_KEY"])
        self.assertEqual(config["update"], "disable")
        self.assertEqual(config["permissions"], [{"action": "*", "resource": "*", "effect": "allow"}])

    def test_legacy_aliases_and_new_name_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("FEATHERLESS_API_KEY=old-key\nFEATHERLESS_MODEL=org/legacy\nFEATHERLESS_BASE_URL=https://old.example/v1\n")
            config = oc.load_config(path)
            self.assertEqual(config["LLM_API_KEY"], "old-key")
            self.assertEqual(config["LLM_MODEL"], "org/legacy")
            self.assertEqual(config["LLM_BASE_URL"], "https://old.example/v1")
            self.assertEqual(config["LLM_PROVIDER"], "featherless")
            for text in ["LLM_API_KEY=new-key\nFEATHERLESS_API_KEY=old-key\n", "FEATHERLESS_API_KEY=old-key\nLLM_API_KEY=new-key\n"]:
                path.write_text(text)
                self.assertEqual(oc.load_config(path)["LLM_API_KEY"], "new-key")
            path.write_text("LLM_API_KEY=\nFEATHERLESS_API_KEY=old-key\n")
            self.assertEqual(oc.load_config(path)["LLM_API_KEY"], "")

    def test_configuration_mode_and_provider_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            for text in ["OPENCODE_CONFIG_MODE=other", "LLM_PROVIDER=a/b", "LLM_PROVIDER={env:KEY}", "LLM_PROVIDER=", "LLM_MODEL={env:KEY}"]:
                path.write_text(text)
                with self.subTest(text=text), self.assertRaises(oc.Error):
                    oc.load_config(path)

    def test_native_transfer_has_no_host_mounts_or_argv_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            env_path = directory / ".env"
            env_path.write_text("OPENCODE_CONFIG_MODE=native\nLLM_API_KEY=secret\n")
            main = '// JSONC remains verbatim\n{"providers": {}, "model": "custom/model"}\n'
            cli = '{"theme": {"name": "opencode"}}\n'
            (directory / "opencode.jsonc").write_text(main)
            (directory / "cli.json").write_text(cli)
            (directory / "unrelated-file").write_text("do not copy")
            config = oc.load_config(env_path)
            command, env = oc.container_command("image", config, capture=True)
            self.assertNotIn("--volume", command)
            self.assertNotIn("secret", " ".join(command))
            self.assertNotIn(main, " ".join(command))
            self.assertEqual(env["FEATHERLESS_API_KEY"], "secret")
            target = directory / "container-copy"
            generator.write_configuration(env, target)
            self.assertEqual(sorted(p.name for p in target.iterdir()), ["cli.json", "opencode.jsonc"])
            self.assertEqual((target / "opencode.jsonc").read_text(), main)
            self.assertEqual((target / "cli.json").read_text(), cli)
            self.assertEqual((target / "opencode.jsonc").stat().st_mode & 0o777, 0o600)
            self.assertEqual((directory / "opencode.jsonc").read_text(), main)

    def test_native_validation_and_env_mode_ignores_native_files(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            path = directory / ".env"
            path.write_text("OPENCODE_CONFIG_MODE=native\n")
            with self.assertRaises(oc.Error):
                oc.load_config(path)
            main = directory / "opencode.json"
            for invalid in ['{"key":"secret",', '[]', '']:
                main.write_text(invalid)
                with self.assertRaises(oc.Error) as exc:
                    oc.load_config(path)
                self.assertNotIn("secret", str(exc.exception))
            main.write_text('{}')
            (directory / "opencode.jsonc").write_text('{}')
            with self.assertRaises(oc.Error):
                oc.load_config(path)
            path.write_text("OPENCODE_CONFIG_MODE=env\n")
            config = oc.load_config(path)
            self.assertNotIn("_native_files", config)
            _, env = oc.container_command("image", config, capture=True)
            self.assertEqual(env["OPENCODE_NATIVE_FILES"], "{}")
            target = directory / "container-copy"
            generator.write_configuration(env, target)
            self.assertEqual(sorted(p.name for p in target.iterdir()), ["opencode.json"])

    def test_native_transfer_rejects_large_payload_and_path_traversal(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "opencode.json").write_text(json.dumps({"description": "x" * 65536}))
            with self.assertRaises(oc.Error):
                oc.native_files(directory)
            with self.assertRaises(ValueError):
                generator.write_configuration({"OPENCODE_CONFIG_MODE": "native", "OPENCODE_NATIVE_FILES": json.dumps({"../escape": "{}"})}, directory / "copy")

    def test_native_check_config_uses_offline_loader_and_no_env_inference_checks(self):
        config = dict(oc.DEFAULTS, OPENCODE_CONFIG_MODE="native", _native_files={"opencode.json": "{}"})
        with patch.object(oc, "rootless"), patch.object(oc, "image_exists", return_value=True), patch.object(oc, "run_container") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = json.dumps([{"type": "document", "path": "/home/agent/.config/opencode/opencode.json", "info": {}}])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                oc.check_config(config)
            run.assert_called_once()
            self.assertTrue(run.call_args.kwargs["offline"])
            self.assertEqual(run.call_args.kwargs["args"], ["debug", "config"])
            self.assertNotIn("INCOMPLETE", output.getvalue())
            run.return_value.returncode = 1
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(oc.Error):
                oc.check_config(config)
            run.return_value.returncode = 0
            run.return_value.stdout = json.dumps([{"type": "directory", "path": "/home/agent/.config/opencode"}])
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(oc.Error):
                oc.check_config(config)

    def test_launch_mounts_and_secrets(self):
        config = dict(oc.DEFAULTS, LLM_API_KEY="private-secret")
        args, env = oc.container_command("image", config, capture=True)
        self.assertNotIn("--volume", args)
        self.assertNotIn("private-secret", " ".join(args))
        self.assertEqual(env["LLM_API_KEY"], "private-secret")
        for forbidden in ["--privileged", "--pid=host", "--network=host"]:
            self.assertNotIn(forbidden, args)
        with tempfile.TemporaryDirectory() as folder:
            args, _ = oc.container_command("image", config, cwd=Path(folder), capture=True)
            self.assertEqual(args.count("--volume"), 1)
            self.assertEqual(args[args.index("--volume") + 1], folder + ":/workspace:rw")
        self.assertIn("--rm", args)
        self.assertIn("--userns=keep-id:uid=1000,gid=1000", args)

    def test_api_missing_credentials_no_network(self):
        with patch("urllib.request.build_opener") as opener, self.assertRaises(oc.Error):
            oc.test_api(oc.DEFAULTS)
        opener.assert_not_called()

    def test_api_one_small_request_and_redaction(self):
        config = dict(oc.DEFAULTS, LLM_API_KEY="secret", LLM_MODEL="org/model")
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps({"choices": [{"message": {"content": "OK secret\u001b"}}]}).encode()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                oc.test_api(config)
            opener.return_value.open.assert_called_once()
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(json.loads(request.data)["max_tokens"], 8)
            self.assertTrue(request.full_url.endswith("/v1/chat/completions"))
            self.assertNotIn("secret", output.getvalue())
            self.assertNotIn("\u001b", output.getvalue())

    def test_api_errors_do_not_echo_remote_secrets(self):
        config = dict(oc.DEFAULTS, LLM_API_KEY="secret", LLM_MODEL="org/model")
        for code, text in [(401, "Authentication"), (403, "Access"), (404, "model-selection"), (422, "request-format"), (429, "quota"), (500, "server"), (302, "redirects")]:
            with self.subTest(code=code), patch("urllib.request.build_opener") as opener:
                opener.return_value.open.side_effect = urllib.error.HTTPError("https://x", code, "secret", {}, None)
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(oc.Error) as exc:
                    oc.test_api(config)
                self.assertIn(text, str(exc.exception))
                self.assertNotIn("secret", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
