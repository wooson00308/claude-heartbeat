# Changelog

이 프로젝트의 주요 변경사항을 기록한다. 형식은 [Keep a Changelog](https://keepachangelog.com/) 1.1.0을 따른다.

## [0.2.1] - 2026-05-14

### Fixed

- heartbeat: condition 불충족 분기에서 `state["last_run"]`이 갱신되지 않아 매 tick마다 condition 재체크가 반복되던 문제. 슬러그 다수 등록 시 1초 안에 수십 줄의 "condition 불충족, 스킵" 로그가 쏟아지고, condition으로 지정된 외부 명령(예: `dream-prep status`)이 매 tick마다 호출되며 누적 부하를 유발했다. 이제 condition 미충족 시에도 `last_run`과 `last_result="skipped"`를 기록해 다음 interval까지 재체크를 미룬다.

### Changed

- dream-prep: `cursor_uuid`가 transcript에 없을 때의 기본 동작이 fail-open(처음부터 재처리)에서 hard fail(`CursorNotFoundError`)로 바뀌었다. 이전엔 LLM이 같은 메시지를 두 번 흡수해 메모리에 중복 항목이 박힐 수 있었다. 명시적으로 재처리를 원할 때만 `--reset-cursor` 플래그를 사용한다.
- dream-prep: `processed_v2` 섹션의 sealed 항목이 200개를 초과하면 가장 오래된 full-form 항목들을 한 줄(`- file: xxx.jsonl`)로 압축하는 GC가 `mark_processed`에서 자동으로 돈다. UUID 36자가 누적되며 `dream_meta.md`가 비대해지는 문제를 봉합한다. 압축된 항목도 "이미 처리됨" 판정에는 그대로 잡힌다. active 항목은 절대 건드리지 않는다.
- dream-prep: `mark_processed`가 `dream_meta.md` 없을 때 warning 후 return 하던 동작을 바꿔, 정규 포맷(`name`/`description`/`type: reference` frontmatter + 빈 `processed_v2` 섹션)으로 자동 초기화한 뒤 마킹을 진행한다. 첫 라운드 메타를 LLM이 비표준 형태(`last_uuid` 누락, `metadata` 블록)로 만들던 부작용을 봉합한다.

### Migration

- 호환성 변경 없음. 기존 `HEARTBEAT.md`, `dream_meta.md`, launchd plist 모두 그대로 동작한다.
- `cursor_uuid` 미스를 의도적으로 무시하던 사용 패턴이 있다면 `dream-prep prep --reset-cursor`로 교체한다.
- GC 임계를 조정하려면 `skills/dream/preprocess.py`의 `SEALED_GC_THRESHOLD` / `SEALED_GC_KEEP_FULL` 상수를 편집한다. 환경변수 노출은 추후 라운드.

## [0.2.0] - 이전

- `dream-preprocessor` v0.1에서 일반 스케줄러로 일반화. `heartbeat` CLI 도입, `dream-heartbeat`은 alias로 유지.
