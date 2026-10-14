# opencode-chat

Run [OpenCode](https://opencode.ai/v2/docs/) in disposable, rootless Podman
containers with OpenAI-compatible LLM providers. The wrapper supports interactive
chat, read/write access to one selected project directory, and persistent or
session-only Debian packages.

OpenCode is pinned to **2.0.28**. The image uses Debian Bookworm and verifies the
published SHA-512 integrity of the standalone binary archive. Supported container
architectures are amd64 and arm64; integration tests have been run on amd64.

## Requirements

- Linux with working rootless Podman and subordinate UID/GID mappings.
- Python 3 and standard shell utilities on the host.
- Network access for image builds, package installation, and model requests.

Verify rootless operation:

```sh
podman info --format '{{.Host.Security.Rootless}}'
```

The output must be `true`. Installation does not require host root privileges,
Node.js, or npm. An API account is not required to build the image or run tests.

## Installation

From the repository checkout:

```sh
./install.sh
export PATH="$HOME/.local/bin:$PATH"
opencode-chat --check-config
```

The installer builds the images, creates `~/.config/opencode-chat/.env` with
permissions `600`, and links the wrapper into `~/.local/bin`. Existing
configuration is preserved. Add the PATH setting to your shell configuration if
needed. Keep the checkout at its installed location because the launcher uses a
symlink to its files.

## Usage

```sh
# Interactive chat with an internal workspace and no host directory mounts
opencode-chat

# Work on one explicitly selected host directory
opencode-chat --cwd="$HOME/projects/example"
opencode-chat --cwd "$PWD"

# Install temporary packages for this session
opencode-chat --cwd="$PWD" --packages=nmap,jq
opencode-chat --packages nmap,jq --packages-file ./tools.conf
opencode-chat --packages-file=./tools.conf

# Forward arguments to OpenCode
opencode-chat -- --help
opencode-chat -- --version
opencode-chat -- run 'Explain this project'
```

Options accept both `--option=value` and `--option value`. Package options can
be repeated. Unknown arguments are forwarded to OpenCode; use `--` when an
OpenCode option conflicts with a wrapper option.

| Option | Behavior |
| --- | --- |
| `--cwd DIR` | Mount the canonical directory read/write at `/workspace`. |
| `--packages LIST` | Install comma-separated packages in the disposable session. |
| `--packages-file FILE` | Read temporary packages from a host file. |
| `--build` | Build or reuse the base and persistent package images. |
| `--check-config` | Check configuration, rootless Podman, and image availability. |
| `--self-test` | Run container integration tests without external inference. |
| `--test-api` | Send one chat-completion request with an eight-token output limit. |
| `--help` | Show wrapper help. |

## Configuration

### Environment mode

The default configuration is `~/.config/opencode-chat/.env`:

```dotenv
OPENCODE_CONFIG_MODE=env
LLM_PROVIDER=featherless
LLM_BASE_URL=https://api.featherless.ai/v1
LLM_API_KEY=
LLM_MODEL=

OPENCODE_PACKAGES=git,curl,jq
```

| Setting | Purpose |
| --- | --- |
| `OPENCODE_CONFIG_MODE` | `env` generates configuration; `native` loads native files. |
| `LLM_PROVIDER` | OpenCode provider ID, such as `featherless` or a custom ID. |
| `LLM_BASE_URL` | OpenAI-compatible API base URL, including its version path. |
| `LLM_API_KEY` | Provider API key. |
| `LLM_MODEL` | Exact model ID expected by the provider. |
| `OPENCODE_PACKAGES` | Packages installed in the persistent derived image. |

Featherless is the initial default. To switch providers, change the provider ID,
base URL, key, and model together. `LLM_PROVIDER` accepts lowercase letters,
digits, underscores, and hyphens. `LLM_MODEL` contains the API model ID without
an added OpenCode provider prefix. For example, `LLM_MODEL=organization/model`
with `LLM_PROVIDER=featherless` selects `featherless/organization/model` in
OpenCode and sends `organization/model` to the API.

Generated configuration enables the selected provider, allows agent tools,
disables updates and sharing, and uses the selected model for the title agent.
The interface can start without credentials; inference requires a configured
provider and model. Select a model with tool-calling support for coding tasks.

The dotenv parser supports assignments, matching quotes, blank lines, and
full-line comments. It does not evaluate shell code, expand variables, or accept
multiline values or inline comments. Unknown settings are rejected.

Existing `FEATHERLESS_BASE_URL`, `FEATHERLESS_API_KEY`, and `FEATHERLESS_MODEL`
settings remain aliases for their `LLM_*` equivalents. New names take precedence
regardless of line order, including explicitly empty values.

### Native mode

Set `OPENCODE_CONFIG_MODE=native` in `.env`. Store either `opencode.json` or
`opencode.jsonc` alongside it in `~/.config/opencode-chat/`. Optional terminal
settings belong in `cli.json` in the same directory.

```sh
cp -n opencode.json.example "$HOME/.config/opencode-chat/opencode.json"
chmod 600 "$HOME/.config/opencode-chat/opencode.json"
# Edit the file and replace the example model ID in all three locations.
# Set OPENCODE_CONFIG_MODE=native in .env and supply LLM_API_KEY as needed.
opencode-chat --check-config
```

For terminal settings, copy `cli.json.example` to the same directory as
`cli.json`. The wrapper rejects simultaneous `opencode.json` and
`opencode.jsonc` files. Environment mode ignores native files.

Native files control OpenCode settings directly; generated provider settings
are not merged into them. A provider can read the shared credential through
`"env": ["LLM_API_KEY"]`. Package settings and CLI options work in both modes.

The host reads only these named files and transfers their contents through
inherited container environment variables. Initialization writes private
container copies with permissions `600`. It adds no host configuration mounts
and preserves host files. The combined transfer limit is 64 KiB. Paths in native
configuration refer to the container filesystem; external host files, plugin
directories, and credential stores are not copied.

Use the [v2 provider schema](https://opencode.ai/v2/docs/providers/),
[v2 configuration documentation](https://opencode.ai/v2/docs/config/), and
[CLI settings schema](https://opencode.ai/v2/docs/cli/config). OpenCode also
normalizes supported v1 settings as described in its
[migration guide](https://opencode.ai/v2/docs/migrate-v1/). V1 plugins require
migration. This wrapper does not copy legacy `tui.json(c)` files.

## Packages

Persistent packages come from `OPENCODE_PACKAGES`. A fingerprint of the base
sources and sorted, deduplicated package list selects the derived image.
Unchanged lists reuse it. Changed lists build from the clean base, so removed
packages disappear unless they are base packages or dependencies. Cached images
remain available for reuse.

Temporary packages come from `--packages` and `--packages-file`. They are
installed with `apt` during initialization and disappear when the session ends.
They do not modify persistent images, host packages, or `.env`. Installation
failure prevents OpenCode from starting.

Package files contain one name per line. Blank lines and lines starting with
`#` are ignored. See [tools.conf](tools.conf). Package names use lowercase
letters, digits, `+`, `.`, and `-`; version expressions and architecture
qualifiers are unsupported.

## Container behavior

Each session uses `--rm`, a writable container filesystem, ordinary rootless
networking, private namespaces, and `/workspace` as the working directory.
Container root installs packages and initializes files, then `setpriv` runs
OpenCode as UID/GID 1000 without privilege escalation. Podman's `keep-id` mapping
makes generated project files belong to the invoking host user.

Chat-only sessions have no host directory bind mounts. Project sessions mount
exactly the directory selected by `--cwd`, read/write. The wrapper rejects the
host root directory and paths containing colons or newlines. It does not
implicitly mount the host home, configuration directories, or container sockets,
and does not enable privileged mode, host PID namespaces, extra capabilities,
or devices.

The selected project is writable and can be modified or deleted by agent tools.
Normal network access is available. API credentials are accessible to the agent
and the host user through container inspection. Internal files, history, saved
credentials, and any OpenCode background server are removed with the container.
The agent cannot use root `apt` after initialization; supply system packages at
startup or install utilities into user-writable container paths.

Rootless containers share the host kernel. Tools requiring raw sockets, packet
capture, kernel access, or devices may not work with the default permissions.
Filesystem tests verify specific isolation behavior, not a general security
guarantee.

## Tests and diagnostics

```sh
python3 -m unittest -v test_launcher
opencode-chat --self-test
opencode-chat --check-config
opencode-chat --test-api
```

Host tests cover argument parsing, dotenv aliases, configuration generation,
native file transfer, and API diagnostics. The 18 container tests cover the
pinned binary, both configuration modes, mock provider requests, bind mounts,
file ownership, package lifetime, image reuse, and container removal. They use
harmless temporary files and dummy credentials. Fixtures and containers are
removed; cached test images remain.

Self-tests do not contact an external inference service. Image builds and
package tests still need network access to fetch archives and Debian packages.
Configuration and mock provider probes run with networking disabled.

`--check-config` sends no API requests and does not build images. Missing
credentials in environment mode are reported as `INCOMPLETE`. In native mode,
it checks that the main file appears as a loaded OpenCode configuration source
when the image is available. OpenCode can normalize or ignore individual fields;
this is not strict whole-document schema validation. Terminal setting types are
checked by the interface at startup.

`--test-api` uses `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` in both modes. It
sends one OpenAI-compatible chat-completion request, refuses redirects, and
redacts the key from displayed responses. It does not derive settings from
arbitrary native providers or test other API formats or tool-call correctness.

For startup failures, check `podman info`, `opencode-chat -- --version`, and
`opencode-chat --check-config`. Package failures include apt output. Bind-mount
access requires the selected directory to be writable by the host user; the
wrapper does not change its ownership. If an execution sandbox blocks Podman's
runtime path, run the same command from an ordinary host terminal.

## Updating

OpenCode's version and both platform archive checksums are pinned in
[Containerfile](Containerfile). To upgrade, update those pins and the expected
version in [self_test.py](self_test.py), then run:

```sh
opencode-chat --build
opencode-chat --self-test
```

The Debian base tag and apt repositories receive updates, so fresh builds can
resolve newer Debian packages. To refresh an unchanged cached setup, remove its
specific base and derived image tags, refresh the Debian base, and rebuild.
Avoid broad image pruning when other Podman projects share the same storage.

## License

Author: Martin Melmuk.

MIT License. Copyright (c) 2026 Martin Melmuk. See [LICENSE](LICENSE).
OpenCode and container dependencies retain their respective licenses.

## Disclaimer

This is an independent project. It is not built by the OpenCode team and is not
affiliated with, endorsed by, or officially supported by OpenCode or its maintainers.
