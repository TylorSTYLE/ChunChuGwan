"""도메인별 정규화 룰(rules.json) 로딩 테스트."""
import json

import pytest

from chunchugwan import config


@pytest.fixture
def rules_file(tmp_path, monkeypatch):
    path = tmp_path / "rules.json"
    monkeypatch.setattr(config, "RULES_PATH", path)
    return path


def test_missing_file_returns_empty(rules_file):
    assert config.load_domain_rules("example.com") == {}


def test_load_rules(rules_file):
    rules_file.write_text(json.dumps({
        "example.com": {"remove_selectors": [".ads"], "remove_line_patterns": ["^관련"]},
    }), encoding="utf-8")
    rules = config.load_domain_rules("example.com")
    assert rules["remove_selectors"] == [".ads"]
    assert config.load_domain_rules("other.com") == {}


def test_www_prefix_fallback(rules_file):
    rules_file.write_text(json.dumps({"example.com": {"remove_selectors": ["#x"]}}),
                          encoding="utf-8")
    assert config.load_domain_rules("www.example.com")["remove_selectors"] == ["#x"]


def test_broken_json_ignored(rules_file):
    rules_file.write_text("{잘못된 json", encoding="utf-8")
    assert config.load_domain_rules("example.com") == {}
