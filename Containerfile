FROM docker.io/library/debian:bookworm-slim

ARG OPENCODE_VERSION=2.0.28
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl python3 util-linux libstdc++6 libgcc-s1 \
    && rm -rf /var/lib/apt/lists/*
# Official standalone binaries: no host Node/npm installation required.
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
      amd64) asset=x64-baseline; checksum=f13fa1e23974427aab1fc54ab9dbf6f03edcc4cc0573294962511c054c371e505485e1c0650122667e1af216be631338083597f6f52fe9c417b19913d2472ce1 ;; \
      arm64) asset=arm64; checksum=04e8d764cb6f62243dc68c1f1d726c780467ef01cdf114ef46f62f30c22c8c838424e62dbf868fbc1e25d019a69e1504859420ae9acf15a9a4503cb49438988b ;; \
      *) echo 'Unsupported architecture (use amd64 or arm64).' >&2; exit 1 ;; \
    esac; \
    curl -fSL --retry 3 "https://registry.npmjs.org/@opencode/cli-linux-${asset}/-/cli-linux-${asset}-${OPENCODE_VERSION}.tgz" -o /tmp/opencode.tgz; \
    echo "$checksum  /tmp/opencode.tgz" | sha512sum -c -; \
    tar -xzf /tmp/opencode.tgz -C /tmp package/bin/opencode; \
    install -m 755 /tmp/package/bin/opencode /usr/local/bin/opencode; rm -rf /tmp/opencode.tgz /tmp/package; \
    opencode --version
RUN groupadd --gid 1000 agent && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash agent \
    && mkdir /workspace && chown agent:agent /workspace
COPY generate-config.py /usr/local/lib/opencode-chat/generate-config.py
COPY container-init.sh /usr/local/bin/container-init
RUN chmod 755 /usr/local/bin/container-init
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/container-init"]
