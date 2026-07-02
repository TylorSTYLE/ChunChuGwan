"""웹 UI 다국어(i18n) 테스트 — 로케일 결정, 번역 폴백, 언어 전환 라우트."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chunchugwan import config, db
from chunchugwan.web import app as web_app
from chunchugwan.web import i18n


@pytest.fixture
def client(tmp_path, monkeypatch):
    """빈 임시 아카이브 위의 TestClient (인증 off — 로케일 동작만 검증)."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    with db.connect():
        pass  # 스키마 생성
    web_app._active_jobs.clear()
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


# ---- translate / 카탈로그 ----


def test_translate_ko_is_identity():
    assert i18n.translate("ko", "아카이브 목록") == "아카이브 목록"


def test_translate_en():
    assert i18n.translate("en", "아카이브 목록") == "Archived pages"


def test_translate_fallback_unknown_string():
    assert i18n.translate("en", "카탈로그에 없는 문장") == "카탈로그에 없는 문장"


def test_translate_params():
    assert i18n.translate("en", "총 {n}건", n=42) == "42 entries"
    assert i18n.translate("ko", "총 {n}건", n=42) == "총 42건"


def test_translate_context():
    assert i18n.translate("en", "변경") == "Changed"
    assert i18n.translate("en", "변경", ctx="action") == "Change"
    # ko 는 ctx 와 무관하게 원문
    assert i18n.translate("ko", "변경", ctx="action") == "변경"


def test_all_template_msgids_have_english():
    """템플릿의 모든 _("...") 리터럴 키가 en 카탈로그에 있어야 한다.

    누락되면 영어 화면에 한국어 원문이 그대로 노출된다. 변수 인자
    (_(label) 등)는 정적으로 못 잡으므로 리터럴만 검사한다.
    """
    call_re = re.compile(
        r"""_\(\s*(['"])(.+?)\1\s*(?:,\s*ctx=(['"])(\w+)\3)?""", re.DOTALL
    )
    catalog = i18n.CATALOGS["en"]
    missing = []
    tpl_dir = Path(i18n.__file__).parent / "templates"
    for tpl in sorted(tpl_dir.glob("*.html")):
        for m in call_re.finditer(tpl.read_text(encoding="utf-8")):
            key = f"{m.group(4)}|{m.group(2)}" if m.group(4) else m.group(2)
            if key not in catalog:
                missing.append(f"{tpl.name}: {key}")
    assert not missing, "en 카탈로그 누락:\n" + "\n".join(missing)


def test_all_spa_msgids_have_english():
    """SvelteKit SPA(.svelte/.ts)의 모든 t('...') 리터럴이 en 카탈로그에 있어야 한다.

    누락되면 영어 화면에 한국어 원문이 그대로 노출된다. 직접 t('...') 리터럴과
    run(fn, '...') 알림 토스트(t(ok) 로 번역)를 검사한다. STATUS_LABELS 같은
    객체 리터럴 값은 정적으로 못 잡으므로 그 키는 카탈로그를 직접 관리한다.
    (#10 i18n 추출 — dashboard 규칙)
    """
    pat_s = re.compile(r"\bt\(\s*'((?:[^'\\]|\\.)*)'")
    pat_d = re.compile(r'\bt\(\s*"((?:[^"\\]|\\.)*)"')
    # run(async () => {...}, '알림 문구') — 화살표 함수가 끝나는 `}, '...'` 패턴
    pat_run = re.compile(r"\},\s*'((?:[^'\\]|\\.)*)'\)")
    catalog = i18n.CATALOGS["en"]
    src = Path(i18n.__file__).resolve().parents[2] / "frontend" / "src"
    if not src.exists():
        pytest.skip("frontend 소스가 없습니다 (패키지 전용 환경)")
    missing = []
    for f in sorted(src.rglob("*")):
        if f.suffix not in (".svelte", ".ts"):
            continue
        text = f.read_text(encoding="utf-8")
        for pat, esc in ((pat_s, "\\'"), (pat_d, '\\"'), (pat_run, "\\'")):
            for m in pat.finditer(text):
                key = m.group(1).replace(esc, esc[-1])
                if key not in catalog:
                    missing.append(f"{f.name}: {key}")
    assert not missing, "en 카탈로그 누락(SPA):\n" + "\n".join(missing)


def test_no_tojson_in_double_quoted_attribute():
    """tojson 값을 큰따옴표 속성 안에 두면 안 된다.

    Jinja `tojson` 은 `<`·`>`·`&`·`'` 만 이스케이프하고 `"` 는 그대로 둔다.
    그래서 `onclick="f({{ x|tojson }})"` 처럼 큰따옴표 속성 안에 넣으면
    JSON 의 `"` 가 속성을 일찍 끊어 핸들러가 깨진다(복사 버튼·confirm 이
    조용히 무력화됨). 작은따옴표 속성(`onclick='f({{ x|tojson }})'`)을 써야
    한다. tojson 호출이 괄호로 닫힌 뒤 큰따옴표가 오는 패턴을 잡는다.
    """
    broken_re = re.compile(r"tojson\s*\}\}\s*\)+\s*\"")
    offenders = []
    tpl_dir = Path(i18n.__file__).parent / "templates"
    for tpl in sorted(tpl_dir.glob("*.html")):
        text = tpl.read_text(encoding="utf-8")
        for ln, line in enumerate(text.splitlines(), 1):
            if broken_re.search(line):
                offenders.append(f"{tpl.name}:{ln}: {line.strip()}")
    assert not offenders, (
        "tojson 값이 큰따옴표 속성 안에 있음 (작은따옴표 속성으로 바꿀 것):\n"
        + "\n".join(offenders)
    )


def test_format_interval():
    assert i18n.format_interval("ko", 3600) == "1시간"
    assert i18n.format_interval("en", 3600) == "1h"
    assert i18n.format_interval("ko", 86400 + 12 * 3600) == "1일 12시간"
    assert i18n.format_interval("en", 86400 + 12 * 3600) == "1d 12h"
    assert i18n.format_interval("en", 0) == "0m"
    # 미지원 로케일은 기본(ko) 단위로 폴백
    assert i18n.format_interval("xx", 3600) == "1시간"


# ---- 로케일 결정 ----


# ---- 언어 설정 (/settings/account/language) ----


# ---- 화면별 영어 렌더링 스모크 ----


