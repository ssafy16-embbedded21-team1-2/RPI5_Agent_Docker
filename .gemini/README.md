# `.gemini/` 설정 안내

이 디렉토리는 **Gemini CLI용 선택 브리지 설정**만 포함합니다.

* [`settings.json`](settings.json)은 Gemini CLI가 루트의 [`AGENTS.md`](../AGENTS.md)를 기본 컨텍스트 파일로 읽도록 지정합니다(중복 문서 `GEMINI.md`를 따로 두지 않습니다).
* Gemini를 사용하지 않는 팀원(Claude Code, Codex, Cursor, 일반 개발자 등)은 이 디렉토리를 별도로 신경 쓰지 않아도 됩니다.
* 개인 PC의 로컬 세션 캐시(`tmp/` 등)와 `settings.local.json`은 `.gitignore`에 의해 Git 추적에서 제외됩니다.
