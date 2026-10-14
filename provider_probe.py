"""Runs only inside a test container: exercise the pinned provider against localhost."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading

requests = []


class MockAPI(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append((self.path, body, self.headers.get("Authorization")))
        self.send_response(200)
        if body.get("stream"):
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delta, finish in [({"role": "assistant", "content": "OK"}, None), ({}, "stop")]:
                chunk = {"id": "mock-1", "object": "chat.completion.chunk", "created": 1,
                         "model": body["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": "mock-1", "object": "chat.completion", "created": 1,
                "model": body["model"], "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode())


server = ThreadingHTTPServer(("127.0.0.1", 0), MockAPI)
threading.Thread(target=server.serve_forever, daemon=True).start()
file = Path("/home/agent/.config/opencode/opencode.json")
config = json.loads(file.read_text())
provider = config.get("model", "featherless/").split("/", 1)[0]
config["providers"][provider]["settings"]["baseURL"] = f"http://127.0.0.1:{server.server_port}/v1"
config["model"] = provider + "/test-organization/offline-model"
config["agents"] = {"title": {"model": config["model"]}}
config["providers"][provider]["models"] = {"test-organization/offline-model": {"name": "Mock"}}
file.write_text(json.dumps(config))
env = dict(os.environ, LLM_API_KEY="mock-offline-key")
try:
    result = subprocess.run(["opencode", "run", "--standalone", "--format", "json", "Reply OK."],
                            env=env, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr[:2000]
    assert requests, "OpenCode did not reach the mock provider"
    assert all(path == "/v1/chat/completions" for path, _, _ in requests), requests
    assert all(body["model"] == "test-organization/offline-model" for _, body, _ in requests), requests
    assert all(auth == "Bearer mock-offline-key" for _, _, auth in requests), requests
    assert "OK" in result.stdout, result.stdout[:2000]
    print("PASS pinned OpenCode provider emits chat completions with the exact model ID and authentication")
finally:
    server.shutdown()
