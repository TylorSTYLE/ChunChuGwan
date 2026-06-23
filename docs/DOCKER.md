# 도커로 실행

> 로컬에 Python/uv 를 설치하지 않고 Docker / Docker Compose 로 실행하는
> 방법. README 의 [도커 빠른 시작](../README.md#도커-빠른-시작)을 더
> 자세히 다룬다. 환경변수 전체 목록은 [AUTHENTICATION.md](AUTHENTICATION.md#환경변수) 참조.
> 윈도우·macOS 에서 GUI 앱으로 처음 구동한다면 아래
> [Docker Desktop으로 설치 및 설정](#docker-desktop으로-설치-및-설정) 절부터 따라 하면 된다.

## Docker Desktop으로 설치 및 설정

> 윈도우·macOS 에서 **Docker Desktop**(GUI 앱)으로 GHCR 사전 빌드 이미지를 받아
> 처음 구동하는 사용자를 위한 단계별 안내. 이 절만 따라 하면 설치 → 이미지 받기 →
> 대시보드 접속 → 크롬 확장 연결까지 끝난다. compose 명령·환경변수·운영 상세는
> 아래 [Docker Compose (권장)](#docker-compose-권장)·[공통 사항](#공통-사항) 절과
> 겹치는 부분을 그대로 참조한다 (여기서는 GUI 관점만 보강한다).

### 1. Docker Desktop 설치

[Docker Desktop 다운로드](https://www.docker.com/products/docker-desktop/) 에서
운영체제에 맞는 설치본을 받는다 (설치 상세는 [공식 가이드](https://docs.docker.com/desktop/)).

- **macOS** — `.dmg` 를 받아 설치한다. **Apple Silicon(M 시리즈)** 과 **Intel** 빌드가
  나뉘므로 칩에 맞는 쪽을 받는다 (애플 메뉴 → "이 Mac에 관하여" 에서 칩 확인).
- **Windows** — 설치 관리자를 실행하고 **WSL2 백엔드**(기본 권장)를 선택한다. WSL2 가
  없으면 설치 관리자 안내나 `wsl --install` 로 먼저 설치한다.

설치 후 Docker Desktop 을 실행해 데몬이 떠 있는지 확인한다 (메뉴 막대/트레이의 고래
아이콘이 "running"). 터미널에서는 다음으로 확인한다:

```bash
docker version          # Server 섹션이 보이면 데몬 동작 중
docker compose version
```

### 2. 권장 리소스 할당

**Settings → Resources** 에서 컨테이너에 할당할 메모리·CPU 를 조정할 수 있다. 페이지
캡처가 chromium 을 띄우고(worker·cli 컨테이너는 `shm_size: 1gb`), 이미지가 약 1.5GB
라서 메모리를 넉넉히 두는 편이 안정적이다. 아래는 **권장값**일 뿐 강제 요구사항이
아니므로 환경에 맞게 조정한다.

- 메모리: **4GB 이상** (크롤·동시 캡처가 몰리면 6–8GB 권장)
- CPU: **2코어 이상**
- 디스크: 이미지 약 1.5GB + 아카이브 데이터(`./archive`) 증가분만큼 여유

캡처가 자주 실패하거나 컨테이너가 강제 종료되면 먼저 메모리 할당을 올려 본다.

### 3. Apple Silicon 참고

이미지는 amd64/arm64 멀티아치라 **Apple Silicon 에서 에뮬레이션 없이 arm64 네이티브**
로 동작한다. 봇 차단 우회용 real Chrome 채널(`WCCG_CAPTURE_CHANNEL: chrome`)은 amd64
전용이지만, arm64 에서는 자동으로 번들 chromium 으로 폴백되므로 켜 둬도 안전하다.

### 4. 실행 파일 준비

저장소를 클론하면 바로 쓸 수 있는 `docker-compose.yml`(`:latest` 이미지 기준)과 크롬
확장 폴더가 들어 있다. compose 파일은 별도 복사 없이 그대로 사용한다.

```bash
git clone https://github.com/TylorSTYLE/ChunChuGwan.git
cd ChunChuGwan
```

관리자 비번·OIDC·SMTP 같은 **개인 설정·시크릿은 추적 파일(`docker-compose.yml`)을
직접 고치지 말고** gitignore 대상인 `.env`(`cp .env.example .env`) 또는
`docker-compose.override.yml` 에 둔다 — 각 서비스가 `env_file: .env` 로 읽고 override 는
compose 가 자동 병합하므로 시크릿이 커밋될 일이 없다 (→ **환경변수 설정**).

### 5. (선택) GHCR 로그인

이미지는 GitHub Container Registry 에 **공개로 게시**되어 있어 보통 `docker login`
없이 바로 받을 수 있다. 받을 때 `401`/`403`/`denied` 가 나면(패키지가 비공개로 바뀐
경우)에만 로그인한다 — GitHub 에서 `read:packages` 스코프 PAT 을 발급해 넣는다:

```bash
# <YOUR_PAT> 자리에 GitHub PAT(read:packages) 를, <YOUR_GITHUB_USERNAME> 자리에
# GitHub 사용자명을 넣는다. 토큰이 명령 히스토리에 남지 않도록 --password-stdin 으로 전달.
echo <YOUR_PAT> | docker login ghcr.io -u <YOUR_GITHUB_USERNAME> --password-stdin
```

### 6. 이미지 받기·기동

```bash
docker compose pull               # GHCR 에서 이미지 내려받기 (최초 수 분, 약 1.5GB)
docker compose up -d dashboard    # 대시보드 + 워커 기동 (depends_on 으로 worker 동반)
```

CLI 하위 명령(`add`/`list`/`history`/`diff` 등)은 아래
[Docker Compose (권장)](#docker-compose-권장) 절을 참조한다.

기본 이미지는 `:latest`(main 푸시마다 갱신)다. 재현성을 위해 버전을 고정하려면
`docker-compose.override.yml` 에서 시맨틱 버전 태그를 지정한다:

```yaml
# docker-compose.override.yml — 버전 고정 (재현성)
services:
  dashboard:
    image: ghcr.io/tylorstyle/chunchugwan:1.2.3   # 원하는 릴리스 버전 (v 접두어 없음)
  worker:
    image: ghcr.io/tylorstyle/chunchugwan:1.2.3
  cli:
    image: ghcr.io/tylorstyle/chunchugwan:1.2.3
```

사용 가능한 태그: `:latest`·`:main`(main 브랜치), `:develop`(테스트), `:1.2.3` 같은
릴리스 버전, `:sha-<커밋>`. 테스트(`develop`) 이미지로 띄우려면 리포의
`docker-compose.dev.yml` 오버라이드를 함께 넘긴다 (→ [Docker Compose (권장)](#docker-compose-권장)).

### 7. 동작 확인

브라우저에서 **http://127.0.0.1:8765** 로 접속한다. 사용자가 한 명도 없는 최초
구동이면 `/setup` 으로 이동해 **관리자 계정 생성**(또는 백업 복원·네트워크 이전)을
진행한다. `docker-compose.override.yml` 에 `WCCG_ADMIN_EMAIL`/`WCCG_ADMIN_PASSWORD`
를 넣어 두면 자동 등록된다.

Docker Desktop **Containers** 탭에서 `dashboard`·`worker` 컨테이너 상태를 보고, 각
컨테이너의 로그와 `8765:8765` 포트 링크를 클릭해 바로 열 수 있다. 터미널에서는:

```bash
docker compose ps                 # 컨테이너 상태
docker compose logs -f dashboard  # 대시보드 로그
docker compose logs -f worker     # 워커(캡처) 로그
```

### 8. 환경변수 설정

`docker-compose.yml` 의 `dashboard.environment` 블록에 자주 쓰는 항목(관리자 자동
등록·공개 URL·OIDC·SMTP)이 주석으로 들어 있다. **필요한 것만 `docker-compose.override.yml`
로 복사해** 값을 채운다 (추적 파일을 직접 고치지 않는다). 변수 전체 목록은
[환경변수](AUTHENTICATION.md#환경변수) 절을 참조한다 (여기서는 중복 나열하지 않는다).

또는 리포의 `.env.example` 을 `.env` 로 복사해(`cp .env.example .env`) `KEY=값` 으로
채워도 된다 — 각 서비스가 `env_file: .env` 로 읽는다(`environment:` 고정값이 우선).

```yaml
# docker-compose.override.yml
services:
  dashboard:
    environment:
      WCCG_ADMIN_EMAIL: "admin@example.com"    # 최초 구동 시 관리자 자동 등록
      WCCG_ADMIN_PASSWORD: "********"           # 8자 이상, 최초 구동 후 제거 권장
      # WCCG_PUBLIC_URL: "https://archive.example.com"   # 리버스 프록시로 외부 노출 시
```

> `WCCG_HOST: "0.0.0.0"` 는 컨테이너 **내부** 바인딩이라 그대로 둔다 — 외부 노출은
> `127.0.0.1:8765` 포트 매핑이 막고, 그래서 컨테이너 대시보드는 인증이 항상 켜진다
> (보안 동작 상세 → [공통 사항](#공통-사항)).

### 9. 크롬 확장 연결

크롬 확장(Manifest V3)은 웹스토어 미등록이라 **압축해제된 확장**으로 직접 로드한다.

1. 크롬 주소창에 `chrome://extensions` → 우상단 **개발자 모드** 켜기.
2. **압축해제된 확장 프로그램 로드** → 클론한 저장소의 `chunchugwan/extension` 폴더 선택.
3. 툴바의 확장 아이콘을 눌러 팝업의 **연결** 탭에서 아래를 입력하고 **연결** 한다:
   - **춘추관 주소** — `http://127.0.0.1:8765`
   - **개인 API Key** — `wccg_…` 로 시작하는 키 (비밀번호가 아니다)

개인 API Key 는 대시보드의 **개인 API Key** 화면에서 발급한다 (`use_api_keys` 권한
필요). 팝업의 "개인 API Key 화면 열기" 버튼으로 그 화면에 바로 갈 수 있다.

연결 후에는 팝업 없이 **기본 단축키**로도 아카이브할 수 있다 — 서버 아카이브
`Ctrl+Shift+S`(macOS `Cmd+Shift+S`), 브라우저 직접 캡처 `Ctrl+Shift+E`(macOS
`Cmd+Shift+E`). 단축키는 `chrome://extensions/shortcuts` 에서 확인·변경한다. 확장
동작 상세는 [API.md](API.md) 참조.

### 데이터·볼륨

- `./archive` — 아카이브 데이터(스냅샷·CAS). `./logs` — 서비스별 로그 파일
  (`dashboard.log`·`worker.log`·`cli.log`). 둘 다 호스트에 바인드 마운트되어 컨테이너를
  지워도 유지된다 (상세 → [공통 사항](#공통-사항)).
- **Windows** — WSL2 백엔드에서는 프로젝트를 **WSL2 파일시스템 안**(리눅스 배포판 홈
  등)에 두면 바인드 마운트 성능이 좋다. `\\wsl$\` 경로로 탐색기에서 접근할 수 있고,
  `/mnt/c/...`(윈도우 드라이브) 아래보다 빠르다.
- Docker Desktop **Volumes** 탭에서 볼륨을 볼 수 있지만, 이 프로젝트는 명명 볼륨이
  아니라 호스트 디렉토리 바인드 마운트라 실제 파일은 위 `./archive`·`./logs` 에 있다.
- 초기화하려면 컨테이너를 내린 뒤 호스트의 `./archive`(및 `./logs`)를 지운다.

### 운영·업데이트

```bash
docker compose down               # 중지 (컨테이너 제거, 데이터는 ./archive 에 유지)
docker compose up -d dashboard    # 재시작
docker compose pull && docker compose up -d dashboard   # 최신 이미지로 업데이트
```

Docker Desktop **Containers** 탭에서 컨테이너를 토글(시작/정지)하거나 로그를 볼 수도
있다. 업데이트는 원하는 태그를 다시 받은 뒤 재기동하면 된다 — 버전을 고정했다면
`docker-compose.override.yml` 의 태그를 올리고 다시 `pull` 한다.

### 문제 해결

- **`401`/`403`/`denied`(pull 실패)** — 패키지가 비공개거나 토큰이 만료됨. 위
  **(선택) GHCR 로그인** 처럼 `docker login ghcr.io`(read:packages PAT)로 로그인한다.
- **`manifest unknown`** — 존재하지 않는 태그. `:latest`·`:develop`·`:1.2.3` 등 실제
  태그인지 확인한다.
- **플랫폼 불일치(`no matching manifest`)** — 멀티아치 이미지라 보통 발생하지 않는다.
  나오면 Docker Desktop 을 최신으로 올리고 다시 받는다.
- **포트 8765 충돌(`address already in use`)** — 호스트의 8765 를 다른 프로그램이 점유
  중. `docker-compose.override.yml` 에서 호스트 쪽 포트만 바꾼다 (컨테이너는 8765 유지):
  ```yaml
  services:
    dashboard:
      ports:
        - "127.0.0.1:9000:8765"   # 이후 http://127.0.0.1:9000 로 접속
  ```
- **메모리 부족(캡처 실패·컨테이너 강제 종료)** — 위 **권장 리소스 할당** 에서 메모리를 올린다.

## Docker Compose (권장)

리포지토리에 바로 쓸 수 있는 `docker-compose.yml` 이 들어 있다. GitHub Actions 가
main 푸시마다 빌드해 GHCR 에 게시하는 이미지(`ghcr.io/tylorstyle/chunchugwan:latest`,
amd64/arm64)를 사용한다.

관리자 비번·OIDC·SMTP 같은 개인 설정·시크릿은 추적 파일을 직접 고치지 말고
gitignore 대상인 `.env`(`KEY=값`) 또는 `docker-compose.override.yml` 에 둔다 — 각
서비스가 `env_file: .env` 로 읽고(`.env` 가 없어도 무방), override 는 compose 가 자동
병합하므로 시크릿이 커밋될 일이 없다. 리포의 [`.env.example`](../.env.example) 을
`.env` 로 복사해 시작하면 된다(`cp .env.example .env`). 고정 운영값(`environment:` 의
`WCCG_HOST` 등)이 `.env` 보다 우선한다. 로컬 소스로 직접 빌드하려면 override 에서
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
