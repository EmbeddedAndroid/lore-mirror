# Official multi-architecture Debian image digest, verified 2026-10-07.
FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587

ARG DEBIAN_SNAPSHOT=20261006T000000Z
ARG PUBLIC_INBOX_COMMIT=94b6bbb95cb88a6de59458b47869c74fc0c901ad
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
      ca-certificates git curl make gcc libc6-dev perl \
      python3 python3-venv python3-pip \
      liburi-perl libdbi-perl libdbd-sqlite3-perl \
      libsearch-xapian-perl libemail-address-xs-perl libsocket-msghdr-perl \
      liblinux-inotify2-perl libnet-server-perl libtimedate-perl \
      libinline-c-perl xapian-tools time && \
    rm -rf /var/lib/apt/lists/*

RUN git init /opt/public-inbox && \
    git -C /opt/public-inbox remote add origin https://kernel.googlesource.com/pub/scm/infra/public-inbox && \
    git -C /opt/public-inbox fetch --depth=1 origin "$PUBLIC_INBOX_COMMIT" && \
    git -C /opt/public-inbox checkout --detach FETCH_HEAD && \
    test "$(git -C /opt/public-inbox rev-parse HEAD)" = "$PUBLIC_INBOX_COMMIT" && \
    cd /opt/public-inbox && perl Makefile.PL && make -j2 && make install && \
    printf '%s\n' "$PUBLIC_INBOX_COMMIT" > /opt/public-inbox-revision

WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --disable-pip-version-check --no-cache-dir \
      --only-binary=:all: --require-hashes -r /app/requirements.lock
COPY src /app/src
COPY tests /app/tests
COPY scripts /app/scripts
COPY config /app/config
COPY README.md /app/README.md
COPY docs /app/docs

RUN groupadd --gid 10001 mirror && useradd --uid 10001 --gid 10001 --home-dir /runtime --no-create-home mirror && \
    mkdir -p /data /issues /runtime/run /runtime/config /runtime/data /runtime/cache && \
    chown -R mirror:mirror /data /issues /runtime && chmod 0700 /runtime/run
USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3).read(4096)"
ENTRYPOINT ["python", "-m", "lore_mirror"]
CMD ["serve", "--config", "/config/pilot.json", "--listen", "0.0.0.0", "--port", "8765"]
