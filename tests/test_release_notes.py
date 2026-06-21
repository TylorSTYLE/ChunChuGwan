"""업데이트 안내 노트 — GitHub 노트 파싱·번들 조회·/api/web/me 노출 검증."""
import json

import pytest
from fastapi.testclient import TestClient

from chunchugwan import config, db
from chunchugwan.web import app as web_app
from chunchugwan.web import i18n, release_notes


@pytest.fixture
def client(tmp_path, monkeypatch):
    """빈 임시 아카이브 위의 TestClient (인증 off)."""
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


# ---- parse_github_notes (GitHub Release 본문 변환) ----

SAMPLE = """릴리스: develop → main (https://github.com/o/r/pull/166)

## What's Changed
* feat: 새 기능 by @alice in https://github.com/o/r/pull/204
* fix: 버그 수정 by @bob in https://github.com/o/r/pull/205
* 릴리스: develop → main by @github-actions[bot] in https://github.com/o/r/pull/166
* 수동 메모 (PR 없음)

**Full Changelog**: https://github.com/o/r/compare/v1...v2
"""


def test_parse_extracts_items_and_filters_noise():
    items = release_notes.parse_github_notes(SAMPLE)
    # 봇 항목·헤더·Full Changelog·머리말 제외 → 3개(alice, bob, 수동 메모)
    assert len(items) == 3
    assert [i["pr"] for i in items] == [204, 205, None]


def test_parse_strips_author_and_raw_link_keeps_pr():
    first = release_notes.parse_github_notes(SAMPLE)[0]
    assert first["text"] == "feat: 새 기능"  # @작성자·링크 제거
    assert "by @" not in first["text"] and "http" not in first["text"]
    assert first["pr"] == 204
    assert first["url"] == "https://github.com/o/r/pull/204"


def test_parse_keeps_bullet_without_pr():
    manual = release_notes.parse_github_notes(SAMPLE)[-1]
    assert manual["text"] == "수동 메모 (PR 없음)"
    assert manual["pr"] is None and manual["url"] is None


def test_parse_excludes_bot_release_pr():
    texts = [i["text"] for i in release_notes.parse_github_notes(SAMPLE)]
    assert all("릴리스: develop" not in t for t in texts)


# ---- note_for (번들 JSON 조회) ----


def test_note_for_known_version_from_bundle():
    note = release_notes.note_for("0.6.1")
    assert note is not None
    assert note["version"] == "0.6.1"
    assert note["items"], "시드된 0.6.1 항목이 있어야 한다"
    item = note["items"][0]
    assert {"text", "pr", "url"} <= set(item)


def test_note_for_unknown_version_is_none():
    assert release_notes.note_for("99.99.99") is None


def test_note_for_strips_local_metadata():
    assert release_notes.note_for("0.6.1+local.1") == release_notes.note_for("0.6.1")


def test_note_for_returns_copy_of_items():
    note = release_notes.note_for("0.6.1")
    note["items"].append({"text": "오염", "pr": None, "url": None})
    assert len(release_notes.note_for("0.6.1")["items"]) != len(note["items"])


def test_bundle_json_is_valid():
    data = json.loads(release_notes.JSON_PATH.read_text(encoding="utf-8"))
    assert "0.6.1" in data and data["0.6.1"]["items"]


# ---- 제목 로케일 ----


def test_title_localized():
    assert i18n.translate("ko", "{version} 새 소식", version="1.2.3") == "1.2.3 새 소식"
    assert i18n.translate("en", "{version} 새 소식", version="1.2.3") == "What's new in 1.2.3"


# ---- /api/web/me 노출 ----


def test_me_includes_release_note_shape(client):
    body = client.get("/api/web/me").json()
    assert "release_note" in body
    note = body["release_note"]
    # 현재 __version__ 에 번들 항목이 있으면 dict, 없으면 None — 둘 다 허용.
    if note is not None:
        assert {"version", "title", "items"} <= set(note)
        assert note["title"]
        if note["items"]:
            assert {"text", "pr", "url"} <= set(note["items"][0])
