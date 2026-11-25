# `.agents/` 공용 에이전트 규칙 · 스킬 안내

이 디렉토리는 **특정 AI 도구(Gemini, Claude, Codex, Cursor, Copilot 등)에 종속되지 않는 프로젝트 공통 규칙과 작업 런북**을 보관합니다.

## 구성

* **[`AGENTS.md`](../AGENTS.md)**: 저장소 루트의 핵심 프로젝트 컨텍스트 (단일 기준 문서)
* **[`rules/`](rules/)**: 모든 작업 시 항상 준수해야 할 설계·보안·테스트 불변식
  * [`01-safety-and-boundaries.md`](rules/01-safety-and-boundaries.md)
  * [`02-can-protocol-and-testing.md`](rules/02-can-protocol-and-testing.md)
* **[`skills/`](skills/)**: 필요할 때만 열어보는 작업별 절차서(`SKILL.md`)
  * [`verify-and-release`](skills/verify-and-release/SKILL.md) — 코드·문서 수정 후 린트/포맷/테스트/마크다운/Jira 커밋 연동 점검
  * [`can-phase2-protocol`](skills/can-phase2-protocol/SKILL.md) — 보드 A·B CAN 디코더·명세·판정·허용 필드 확장 절차
  * [`rpi5-ops-and-deploy`](skills/rpi5-ops-and-deploy/SKILL.md) — RPi 5 배포, `systemd` 점검, `can0` 및 스모크 테스트 절차

## 도구별 연동 방식

* **Antigravity / Codex / Cursor / Windsurf / Copilot**: 루트 `AGENTS.md` 및 `.agents/`를 직접 인식합니다.
* **Claude Code**: 루트 [`CLAUDE.md`](../CLAUDE.md)가 `@AGENTS.md`를 임포트해 동일 컨텍스트를 공유합니다.
* **Gemini CLI**: [`.gemini/settings.json`](../.gemini/settings.json)이 `AGENTS.md`를 컨텍스트 파일로 우선 로드합니다(Gemini 미사용 팀원은 `.gemini/`를 무시해도 무방합니다).
