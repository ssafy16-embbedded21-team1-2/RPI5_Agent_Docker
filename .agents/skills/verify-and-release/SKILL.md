---
name: verify-and-release
description: >-
  Use when finishing code, configuration, or documentation changes in RPI5_Agent_Docker
  to run lint, formatting, pytest, Docker Compose validation, Markdown checks, secret
  hygiene checks, and Jira commit linking before committing or pushing.
---

# Verify and Release Runbook (`RPI5_Agent_Docker`)

코드·설정·문서 변경을 커밋하거나 푸시하기 전에 아래 검증을 순서대로 실행합니다.

## Steps

1. **코드 품질 및 전체 테스트 실행**
   ```bash
   uv run --frozen ruff check .
   uv run --frozen ruff format --check .
   uv run --frozen pytest -q
   docker compose config --quiet
   ```
   * 하나라도 실패하면 수정 후 마지막 편집 이후 시점에 다시 실행합니다.

2. **마크다운 문법·링크·비밀 정보 점검**
   * 수정한 `.md` 파일의 본문에서 이스케이프되지 않은 `~` 쌍이 GFM 취소선으로 렌더링되지 않는지 확인합니다(백틱 내부 `` `0~100` ``에는 백슬래시를 넣지 않음).
   * `git diff`에서 `.env`, API 키, 비밀번호, 개인 PC 절대 경로가 포함되지 않았는지 확인합니다.

3. **커밋 및 푸시**
   * Jira 이슈 키가 있으면 커밋 메시지에 반영합니다(예: `PJT-27: ...`).
   * `git push origin main` 후 GitHub Actions CI 통과 여부를 확인합니다.

4. **(Jira 연동 시) 커밋 링크 연결**
   * Bitbucket 기본값 방지를 위해 반드시 `--application-name GitHub --application-type com.github` 옵션을 명시합니다.
   ```bash
   twg jira workitem link commit --id PJT-27 \
     --target "https://github.com/ssafy16-embbedded21-team1-2/RPI5_Agent_Docker/commit/<SHA>" \
     --title "RPI5_Agent_Docker@<SHORT_SHA>: <요약>" \
     --application-name GitHub --application-type com.github
   ```
