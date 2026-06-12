"""웹 UI 다국어(i18n) 테스트 — 로케일 결정, 번역 폴백, 언어 전환 라우트."""
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chunchugwan import config, db, storage
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


def test_format_interval():
    assert i18n.format_interval("ko", 3600) == "1시간"
    assert i18n.format_interval("en", 3600) == "1h"
    assert i18n.format_interval("ko", 86400 + 12 * 3600) == "1일 12시간"
    assert i18n.format_interval("en", 86400 + 12 * 3600) == "1d 12h"
    assert i18n.format_interval("en", 0) == "0m"
    # 미지원 로케일은 기본(ko) 단위로 폴백
    assert i18n.format_interval("xx", 3600) == "1시간"


# ---- 로케일 결정 ----


def test_default_locale_is_korean(client):
    res = client.get("/archives")
    assert res.status_code == 200
    assert "아카이브 목록" in res.text
    assert '<html lang="ko">' in res.text


def test_accept_language_english(client):
    res = client.get("/archives", headers={"Accept-Language": "en-US,en;q=0.9"})
    assert res.status_code == 200
    assert "Archived pages" in res.text
    assert '<html lang="en">' in res.text


def test_accept_language_q_priority(client):
    """q 값이 높은 지원 언어를 고른다 (ko;q=0.8 < en;q=0.9)."""
    res = client.get(
        "/archives", headers={"Accept-Language": "fr;q=1.0, ko;q=0.8, en;q=0.9"}
    )
    assert "Archived pages" in res.text


def test_unsupported_accept_language_falls_back(client):
    res = client.get("/archives", headers={"Accept-Language": "fr-FR,de;q=0.8"})
    assert "아카이브 목록" in res.text


# ---- 언어 설정 (/settings/account/language) ----


def test_lang_no_route(client):
    """/lang 엔드포인트는 제거되었다."""
    assert client.post("/lang", data={"lang": "en", "next": "/"}).status_code == 404


# ---- 화면별 영어 렌더링 스모크 ----


def test_dashboard_english(client):
    res = client.get("/", headers={"Accept-Language": "en"})
    assert res.status_code == 200
    assert "Storage trend" in res.text
    assert "Total snapshots" in res.text


def test_login_page_english(monkeypatch, client):
    """인증 켠 상태의 로그인 화면도 영어로 렌더링된다 (쿠키 없이 헤더만으로)."""
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    with db.connect() as conn:
        db.create_first_admin(conn, "a@b.c", "hash")
    res = client.get("/login", headers={"Accept-Language": "en"})
    assert res.status_code == 200
    assert "Log in" in res.text


def test_schedule_label_locale(client):
    """주기 라벨이 로케일에 맞게 표기된다 (목록 화면의 자동 컬럼)."""
    url = "https://example.com/i18n"
    with db.connect() as conn:
        page_id = db.get_or_create_page(
            conn, url, "example.com", storage.url_to_slug(url)
        )
        snap_dir = storage.page_dir("example.com", storage.url_to_slug(url)) / "2026-06-01T00-00-00"
        snap_dir.mkdir(parents=True)
        db.insert_snapshot(
            conn, page_id,
            taken_at="2026-06-01T00:00:00+00:00", dir_name="2026-06-01T00-00-00",
            content_hash="0" * 64, final_url=url, http_status=200, changed=1,
        )
        db.upsert_schedule(
            conn, page_id, 12 * 3600,
            next_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        site = db.get_site_by_key(conn, "example.com")
    assert "12시간" in client.get(f"/sites/{site['id']}").text
    assert "12h" in client.get(
        f"/sites/{site['id']}", headers={"Accept-Language": "en"}
    ).text
