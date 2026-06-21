# 도커로 실행

> 로컬에 Python/uv 를 설치하지 않고 Docker / Docker Compose 로 실행하는
> 방법. README 의 [도커 빠른 시작](../README.md#도커-빠른-시작)을 더
> 자세히 다룬다. 환경변수 전체 목록은 [AUTHENTICATION.md](AUTHENTICATION.md#환경변수) 참조.

## Docker Compose (권장)

리포지토리에 바로 쓸 수 있는 `docker-compose.yml` 이 들어 있다. GitHub Actions 가
main 푸시마다 빌드해 GHCR 에 게시하는 이미지(`ghcr.io/tylorstyle/chunchugwan:latest`,
amd64/arm64)를 사용한다.

관리자 비번·OIDC·SMTP 같은 개인 설정·시크릿은 추적 파일을 직접 고치지 말고
gitignore 대상인 `docker-compose.override.yml`(또는 `.env`)에 둔다 — compose 가
자동 병합하므로 시크릿이 커밋될 일이 없다. 로컬 소스로 직접 빌드하려면 override 에서
`image:` 대신 `build: .` 를 지정한다.

테스트(`develop`) 이미지로 띄우려면 `docker-compose.dev.yml` 오버라이드를 함께 넘긴다:
`docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d`.

```bash
docker compose up -d dashboard         # 대시보드 + 워커 (http://127.0.0.1:8765)
docker compose run --rm cli add <url>  # 스냅샷 생성
docker compose run --rm cli list       # 아카이브 현황
docker compose run --rm cli history <url>  # 스냅샷 목록
docker compose run --rm cli diff <url>     # 스냅샷 비교
docker compose down                    # 대시보드 중지
```

설정은 `docker-compose.override.yml` 의 `environment:` 블록에서 한다. 베이스
`docker-compose.yml` 에 자주 쓰는 항목(관리자 자동 등록, 공개 URL, OIDC, SMTP)이
주석으로 들어 있으니 필요한 것만 override 로 복사하면 된다 — 전체 목록은
[환경변수](AUTHENTICATION.md#환경변수) 절 참조.

```yaml
    environment:
      WCCG_HOST: "0.0.0.0"             # 그대로 둘 것 (컨테이너 내부 바인딩)
      WCCG_ADMIN_EMAIL: "admin@example.com"   # 최초 구동 시 관리자 자동 등록
      WCCG_ADMIN_PASSWORD: "********"         # 8자 이상, 최초 구동 후 제거 권장
```

## Docker 단독 (compose 없이)

```bash
docker build -t chunchugwan .

# 대시보드
docker run -d --name wccg --init --shm-size 1g \
  -e WCCG_HOST=0.0.0.0 \
  -p 127.0.0.1:8765:8765 \
  -v "$PWD/archive:/data/archive" \
  --restart unless-stopped \
  chunchugwan serve

# CLI (대시보드와 같은 ./archive 를 공유)
docker run --rm --init --shm-size 1g \
  -v "$PWD/archive:/data/archive" \
  chunchugwan add <url>
```

환경변수는 `-e WCCG_...=값` 을 추가해 설정한다. 시크릿(OIDC·SMTP 등)이
명령 히스토리에 남는 게 싫으면 `--env-file` 로 gitignore 된 `.env` 파일을
넘기면 된다.

## 공통 사항

- 아카이브 데이터는 호스트의 `./archive` 디렉토리에 바인드 마운트로 저장된다
  (컨테이너를 지워도 유지되며, 로컬 `uv run wccg` 와 같은 데이터를 공유).
- 포트는 호스트의 **127.0.0.1 에만** 바인딩되어 localhost 전용 원칙이 유지된다.
  외부에 노출하려면 포트 매핑을 여는 대신 리버스 프록시(HTTPS 종료)를 앞단에
  두고 `WCCG_PUBLIC_URL` 을 설정할 것.
- 컨테이너 대시보드는 내부적으로 0.0.0.0 바인딩이라 **인증이 항상 켜진다**
  (`WCCG_AUTH=off` 는 loopback 바인딩 전용). 첫 접속 시 `/setup` 에서 관리자를
  등록하거나, `WCCG_ADMIN_EMAIL`/`WCCG_ADMIN_PASSWORD` 로 자동 등록한다.
- 컨테이너는 비루트(uid 1000)로 실행되어 chromium 샌드박스가 활성 상태로 동작한다.
  호스트의 `./archive` 소유자가 달라도(예: docker 가 root 로 자동 생성) 기동 시
  엔트리포인트가 소유자를 uid 1000 으로 보정한 뒤 비루트로 전환하므로 별도
  조치가 필요 없다. 호스트에서 같은 디렉토리를 `uv run wccg` 로 함께 쓰는
  경우에만 파일이 uid 1000 소유가 된다는 점을 참고.
- 최초 빌드는 chromium 다운로드를 포함해 수 분 걸린다 (이미지 약 1.5GB).
