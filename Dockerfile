# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# 의존성 레이어 — uv.lock/pyproject.toml 변경 시에만 무효화
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

# chromium + OS 의존성 — 브라우저 버전은 uv.lock 의 playwright 가 결정
# gosu 는 엔트리포인트의 비루트 강등용. 비루트 사용자도 읽을 수 있도록 권한 보정
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && /app/.venv/bin/playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

COPY pyproject.toml uv.lock ./
COPY chunchugwan/ chunchugwan/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

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
