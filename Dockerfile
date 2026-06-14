# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# 의존성 레이어 — uv.lock/pyproject.toml 변경 시에만 무효화.
# --extra stealth: patchright(스텔스 캡처 엔진) 포함 — 기본 캡처에는 안 쓰이고
# WCCG_CAPTURE_ENGINE=patchright 로 켤 때만 사용된다 (browser_engine.py).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev --extra stealth

# chromium + OS 의존성 — 브라우저 버전은 uv.lock 의 playwright 가 결정.
# gosu: 엔트리포인트의 비루트 강등용. xvfb: 헤드풀 스텔스 캡처
# (WCCG_CAPTURE_HEADFUL=on)용 가상 디스플레이 — 서버엔 물리 디스플레이가 없다.
# google-chrome-stable: patchright 스텔스 경로의 WCCG_CAPTURE_CHANNEL=chrome 용
# (번들 chromium 보다 TLS/HTTP2 지문이 진짜라 네트워크 레벨 탐지에 강하다).
# Google 은 amd64 .deb 만 제공하므로 amd64 에서만 설치하고, arm64 는 번들
# chromium 을 쓴다 (그 경우 WCCG_CAPTURE_CHANNEL 은 비워 둘 것).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends gosu xvfb; \
    if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
        apt-get install -y --no-install-recommends wget gnupg ca-certificates; \
        wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb; \
        apt-get install -y --no-install-recommends /tmp/chrome.deb; \
        rm /tmp/chrome.deb; \
    fi; \
    /app/.venv/bin/playwright install --with-deps chromium; \
    rm -rf /var/lib/apt/lists/*; \
    chmod -R a+rX /ms-playwright

COPY pyproject.toml uv.lock ./
COPY chunchugwan/ chunchugwan/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra stealth

# 비루트 실행 — chromium 샌드박스를 --no-sandbox 없이 유지.
# USER 지시어 대신 엔트리포인트가 root 로 시작해 바인드 마운트된
# /data/archive 소유자를 보정한 뒤 gosu 로 wccg 가 되어 실행한다.
RUN useradd -m -u 1000 wccg \
    && mkdir -p /data/archive \
    && chown -R wccg:wccg /data
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH" \
    WCCG_ROOT=/data/archive

EXPOSE 8765
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["serve"]
