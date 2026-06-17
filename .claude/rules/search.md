---
description: 전문 검색 — FTS5 trigram 색인·문서 본문 추출·reindex/verify. 검색 색인/문서텍스트 모듈을 만질 때.
paths:
  - "chunchugwan/searchindex.py"
  - "chunchugwan/doctext.py"
  - "docs/SEARCH.md"
---

# 전문 검색

- 전문 검색은 FTS5 trigram 가상테이블 — `searchindex.py`.
- 문서 본문 추출(검색 색인): pypdf(PDF) + 표준 zipfile/XML(docx·pptx·xlsx·
  odf·hwpx·epub) — `doctext.py`.

## 관련 DB 테이블

- `snapshot_fts` — 전문 검색 FTS5 가상테이블 (rowid=snapshots.id, 컬럼
  content/title/url, tokenize=trigram). 색인 본문 = content.md(정규화 텍스트)
  + 첨부 문서 본문(doctext.py: PDF·OOXML·ODF·HWPX·EPUB). 쓰기/조회 SQL 은
  db.py 가 소유하고(원칙 1), 텍스트 조립·쿼리 해석·스니펫은 searchindex.py.
  신규 스냅샷은 pipeline 이 저장 시 색인(search_indexed=1), 구형·가져온·실패
  스냅샷은 `wccg search reindex` 백필. 삭제는 db.delete_snapshot/delete_page
  가 함께 제거. compact 가 구형 files/ 문서를 CAS 로 이전하면 그 스냅샷을
  search_indexed=0 으로 되돌려(self-heal) 다음 reindex 가 문서 본문을 잡는다.
  플래그와 FTS 행의 불일치(과소 색인·orphan)는 searchindex.verify/repair
  (`wccg search verify [--repair]`, 시스템 메뉴 "전체 다시 색인" 버튼=
  reindex_all)로 점검·교정. 한국어는 trigram 부분문자열(3글자+), 1~2글자는 LIKE 폴백.
  FTS5 없는 SQLite 빌드에서는 생성이 실패해도 검색만 비활성(graceful) —
  기존 아카이빙은 영향 없음. 재생성 가능한 파생 데이터라 `export` 제외
