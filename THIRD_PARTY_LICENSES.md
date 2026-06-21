# 서드파티 라이선스

춘추관(ChunChuGwan)은 [MIT 라이선스](LICENSE)로 배포된다. 아래는 함께 배포되거나
빌드에 사용되는 서드파티 의존성의 라이선스 목록이다.

## 요약

- **런타임 의존성은 모두 MIT 호환 permissive 라이선스다** (MIT · BSD-2/3-Clause ·
  Apache-2.0 · PSF-2.0 · MIT-CMU · ISC). GPL/LGPL/AGPL 등 강한 카피레프트는 **없다**.
- 유일한 예외는 **`certifi` (MPL-2.0)** — 약한(파일 단위) 카피레프트다. Mozilla 의
  CA 인증서 번들을 그대로 재배포하는 것은 MIT 배포와 충돌하지 않으며, 의무(소스
  공개)는 **certifi 자체 파일을 수정해 배포할 때만** 발생한다. 춘추관은 certifi 를
  수정하지 않으므로 추가 의무가 없다.
- 프론트엔드(SvelteKit) 의존성은 전부 **빌드 타임 devDependency** 로, 컴파일된 정적
  산출물만 배포된다. 라이선스는 MIT 7종 + TypeScript(Apache-2.0, 컴파일러).

이 표는 `wccg`(`pyproject.toml`)의 기본 런타임 의존성 폐포(transitive 포함)를 기준으로
한다. 옵션 extra(`stealth` = `patchright`, Apache-2.0)는 기본 설치에 포함되지 않는다.

## 런타임 의존성 (Python)

`*` = `pyproject.toml` 에 직접 선언된 의존성 / 나머지는 전이(transitive) 의존성.

| 패키지 | 라이선스 | 직접 |
|---|---|:--:|
| argon2-cffi | MIT | * |
| click | BSD-3-Clause | * |
| cryptography | Apache-2.0 OR BSD-3-Clause | * |
| fastapi | MIT | * |
| httpx | BSD-3-Clause | * |
| lxml | BSD-3-Clause | * |
| pillow | MIT-CMU | * |
| playwright | Apache-2.0 | * |
| PyJWT | MIT | * |
| pyotp | MIT | * |
| pypdf | BSD-3-Clause | * |
| python-multipart | Apache-2.0 | * |
| qrcode | BSD-3-Clause [^qrcode] | * |
| uvicorn | BSD-3-Clause | * |
| webauthn | BSD-3-Clause | * |
| annotated-doc | MIT | |
| annotated-types | MIT | |
| anyio | MIT | |
| argon2-cffi-bindings | MIT | |
| cbor2 | MIT | |
| **certifi** | **MPL-2.0** [^certifi] | |
| cffi | MIT | |
| greenlet | MIT AND PSF-2.0 | |
| h11 | MIT | |
| httpcore | BSD-3-Clause | |
| idna | BSD-3-Clause | |
| pyasn1 | BSD-2-Clause | |
| pycparser | BSD-3-Clause | |
| pydantic | MIT | |
| pydantic-core | MIT | |
| pyee | MIT | |
| pyOpenSSL | Apache-2.0 | |
| starlette | BSD-3-Clause | |
| typing-extensions | PSF-2.0 | |
| typing-inspection | MIT | |

## 빌드 의존성 (프론트엔드, devDependencies)

`frontend/package.json`. 빌드 타임에만 쓰이며 런타임에 배포되지 않는다. svelte 런타임은
컴파일되어 정적 산출물에 포함된다(MIT).

| 패키지 | 라이선스 |
|---|---|
| svelte | MIT |
| @sveltejs/kit | MIT |
| @sveltejs/adapter-static | MIT |
| @sveltejs/vite-plugin-svelte | MIT |
| svelte-check | MIT |
| vite | MIT |
| @types/node | MIT |
| typescript | Apache-2.0 |

## 각주

[^certifi]: **MPL-2.0** 은 약한(파일 단위) 카피레프트다. certifi 는 Mozilla 의 신뢰
    CA 인증서 묶음으로, 원본 그대로 재배포하는 한 추가 의무가 없다. MPL-2.0 파일을
    **수정**해 배포하는 경우에만 해당 파일의 소스를 공개할 의무가 생긴다. 춘추관은
    certifi 를 수정하지 않는다. MPL-2.0 은 MIT 와도, (Secondary License 조항으로)
    GPL 계열과도 호환된다.

[^qrcode]: qrcode 패키지 메타데이터에는 `License :: Other/Proprietary License` 분류자가
    같이 달려 있으나, 이는 업스트림 메타데이터 artifact 이고 실제 라이선스는 BSD 다
    (`License: BSD`). 코드 자체는 BSD-3-Clause.

## 갱신 방법

이 목록과 기계 판독용 SBOM(CycloneDX)은 설치된 런타임 환경의 패키지 메타데이터에서
생성한다. 의존성을 추가/변경하면 아래로 재생성한다.

```bash
uv sync --frozen --no-dev                       # 런타임 전용 환경
uvx --from cyclonedx-bom cyclonedx-py environment .venv/bin/python \
    --of JSON -o sbom.cdx.json --pyproject pyproject.toml
uv sync --frozen                                # dev 의존성 복원
```

릴리스 시에는 `release.yml` 이 동일한 명령으로 SBOM(`chunchugwan-vX.Y.Z.cdx.json`)을
생성해 해당 GitHub Release 에 자산으로 자동 첨부한다.
