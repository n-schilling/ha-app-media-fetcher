# Home Assistant's Debian base of the 2026.11 release, pinned by digest;
# Dependabot proposes newer ones
FROM ghcr.io/home-assistant/base-debian:trixie-2026.11.0@sha256:d996c8f8ef0096a1ac3756766149b31b8171afc663068cc192f2bcab20f1b8c9

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Debian's ffmpeg for conversions; yt-dlp and Deno (its JavaScript runtime)
# come from PyPI into a virtual environment, so they can be updated
# independently of the base image
RUN \
    apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# requirements.txt pins every package with its hashes (uv pip compile from
# requirements.in), so a build installs exactly the files reviewed
COPY requirements.txt /tmp/
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --only-binary=:all: --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY rootfs /
RUN /opt/venv/bin/python3 -m compileall -q /usr/lib/media_fetcher

ARG BUILD_ARCH BUILD_DATE BUILD_DESCRIPTION BUILD_NAME BUILD_REF BUILD_REPOSITORY BUILD_VERSION
# yt-dlp finds the deno binary of the virtual environment on the PATH
ENV PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# For the health endpoint
ENV APP_VERSION=${BUILD_VERSION}
# s6-overlay: warnings and errors only, no per-service start/stop info lines
ENV S6_VERBOSITY=1
# The Supervisor restarts the app when the service stops answering
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD /opt/venv/bin/python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/api/v1/health', timeout=5)" || exit 1
LABEL \
    io.hass.name="${BUILD_NAME}" \
    io.hass.description="${BUILD_DESCRIPTION}" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version=${BUILD_VERSION}
