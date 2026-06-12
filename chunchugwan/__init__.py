"""춘추관 — 개인 웹 아카이빙 시스템."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("chunchugwan")
except PackageNotFoundError:  # 설치 전 소스 트리에서 import 한 경우
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
