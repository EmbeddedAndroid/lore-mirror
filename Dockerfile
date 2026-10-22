# The bounded offline code-search helper and indexer share an exact upstream pin.
# Go archives are verified against https://go.dev/dl/?mode=json&include=all.
FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587 AS zoekt-build
ARG DEBIAN_SNAPSHOT=20261006T000000Z
ARG TARGETARCH
RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/%s/ bookworm main\n' "$DEBIAN_SNAPSHOT" > /etc/apt/sources.list && \
    apt-get update && apt-get install --no-install-recommends -y ca-certificates curl git gcc libc6-dev && \
    rm -rf /var/lib/apt/lists/*
RUN set -eu; arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) sum=5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053 ;; \
      arm64) sum=fe4789e92b1f33358680864bbe8704289e7bb5fc207d80623c308935bd696d49 ;; \
      *) echo "unsupported Go build architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl --fail --show-error --location --proto '=https' --proto-redir '=https' \
      "https://go.dev/dl/go1.26.5.linux-${arch}.tar.gz" -o /tmp/go.tar.gz; \
    echo "$sum  /tmp/go.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/go.tar.gz -C /usr/local; rm /tmp/go.tar.gz
ENV PATH=/usr/local/go/bin:$PATH GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /build/zoekt
COPY tools/zoekt/go.mod tools/zoekt/go.sum ./
RUN go mod download
COPY tools/zoekt/ ./
RUN mkdir -p /out && \
    cp "$(go list -m -f '{{.Dir}}' github.com/sourcegraph/zoekt)/LICENSE" /out/zoekt-LICENSE && \
    go build -mod=readonly -trimpath -o /out/lore-zoekt . && \
    go build -mod=readonly -trimpath -o /out/zoekt-git-index github.com/sourcegraph/zoekt/cmd/zoekt-git-index && \
    printf '{"upstream_commit":"e54cd91d1a64864fda7e681c9d31171eaab09ca7","indexer_sha256":"%s","query_bridge_sha256":"%s"}\n' \
      "$(sha256sum /out/zoekt-git-index | cut -d' ' -f1)" \
      "$(sha256sum /out/lore-zoekt | cut -d' ' -f1)" > /out/zoekt-build.json

# Official multi-architecture Debian image digest, verified 2026-10-07.
FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587

ARG DEBIAN_SNAPSHOT=20261006T000000Z
ARG PUBLIC_INBOX_COMMIT=d6b9850df271f3fed85f6d222ec39249d8bc061f
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src PATH=/opt/venv/bin:$PATH \
    HOME=/runtime XDG_CONFIG_HOME=/runtime/config \
    XDG_DATA_HOME=/runtime/data XDG_CACHE_HOME=/runtime/cache \
    XDG_RUNTIME_DIR=/runtime/run

# Freeze distro dependency inputs as well as the public-inbox revision.
# Refresh the snapshot and base digest together for security updates.
RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/%s/ bookworm main\n' "$DEBIAN_SNAPSHOT" > /etc/apt/sources.list && \
    printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/%s/ bookworm-security main\n' "$DEBIAN_SNAPSHOT" >> /etc/apt/sources.list && \
    apt-get update && apt-get install --no-install-recommends -y \
      ca-certificates git curl make gcc g++ libc6-dev perl socat \
      python3 python3-venv python3-pip \
      liburi-perl libdbi-perl libdbd-sqlite3-perl \
      libsearch-xapian-perl libemail-address-xs-perl libsocket-msghdr-perl \
      liblinux-inotify2-perl libnet-server-perl libtimedate-perl \
      libinline-c-perl xapian-tools time universal-ctags tini && \
    rm -rf /var/lib/apt/lists/*

RUN git init /opt/public-inbox && \
    git -C /opt/public-inbox remote add origin https://kernel.googlesource.com/pub/scm/infra/public-inbox && \
    git -C /opt/public-inbox fetch --depth=1 origin "$PUBLIC_INBOX_COMMIT" && \
    git -C /opt/public-inbox checkout --detach FETCH_HEAD && \
    test "$(git -C /opt/public-inbox rev-parse HEAD)" = "$PUBLIC_INBOX_COMMIT" && \
    cd /opt/public-inbox && perl Makefile.PL && make -j2 && make install && \
    printf '%s\n' "$PUBLIC_INBOX_COMMIT" > /opt/public-inbox-revision

COPY --from=zoekt-build /out/lore-zoekt /out/zoekt-git-index /usr/local/bin/
COPY --from=zoekt-build /out/zoekt-build.json /usr/local/share/lore-mirror/zoekt-build.json
COPY --from=zoekt-build /out/zoekt-LICENSE /usr/local/share/lore-mirror/zoekt-LICENSE
RUN python3 -c 'import hashlib,json,pathlib; p=pathlib.Path("/usr/local/share/lore-mirror/zoekt-build.json"); d=json.loads(p.read_text()); d["ctags_sha256"]=hashlib.sha256(pathlib.Path("/usr/bin/ctags-universal").read_bytes()).hexdigest(); p.write_text(json.dumps(d)+"\n")'

WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --disable-pip-version-check --no-cache-dir \
      --only-binary=:all: --require-hashes -r /app/requirements.lock
COPY src /app/src
COPY tests /app/tests
COPY tools/zoekt/integration_test.py /app/tools/zoekt/integration_test.py
COPY scripts /app/scripts
COPY config /app/config
# Offline deployment-contract tests read every checked-in Compose overlay.
COPY compose*.yaml /app/
COPY README.md /app/README.md
COPY docs /app/docs

RUN groupadd --gid 10001 mirror && useradd --uid 10001 --gid 10001 --home-dir /runtime --no-create-home mirror && \
    mkdir -p /data /issues /runtime/run /runtime/config /runtime/data /runtime/cache && \
    chown -R mirror:mirror /data /issues /runtime && chmod 0700 /runtime/run
USER 10001:10001
RUN perl -MPublicInbox::LEI -e 1 && python -m pip check
ARG LORE_BUILD_FINGERPRINT=development
ENV LORE_BUILD_FINGERPRINT=$LORE_BUILD_FINGERPRINT
LABEL org.opencontainers.image.version=$LORE_BUILD_FINGERPRINT
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python /app/scripts/healthcheck.py api
ENTRYPOINT ["python", "-m", "lore_mirror"]
CMD ["serve", "--config", "/config/pilot.json", "--listen", "0.0.0.0", "--port", "8765"]
