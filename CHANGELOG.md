# Changelog

이 프로젝트의 주요 변경사항을 기록한다. 형식은 [Keep a Changelog](https://keepachangelog.com/) 1.1.0을 따른다.

## [0.5.0] - 2026-05-15

### Added

- Linux systemd 통합. `heartbeat install-service`가 Linux에서 `~/.config/systemd/user/claude-heartbeat.service` user unit을 자동 생성하고 `systemctl --user daemon-reload && systemctl --user enable --now claude-heartbeat.service`까지 실행한다. 로그아웃 후에도 돌리려면 `loginctl enable-linger $USER`를 별도 안내.
- `heartbeat uninstall-service`가 Linux에서 `disable --now` + unit 파일 삭제 + `daemon-reload`까지 처리.
- `--print-only` 모드에서 systemd unit 내용과 install/enable 명령을 출력 (실제 등록 없이 검증 가능).
- 테스트: `test_install_service_print_only_linux` 추가. unit 헤더 / heartbeat 경로 / systemctl enable 명령 / linger 안내가 모두 출력에 포함됨을 검증.

### Changed

- `service.py`의 install/uninstall 디스패처가 macOS / Windows / Linux 3-way 분기로 통일. 더 이상 "Phase 3 예정" 자리표시자 없음.
- README Prerequisites: "macOS / Windows / Linux".

### Migration

- 호환성 변경 없음. 기존 macOS / Windows 사용자에 영향 0.
- Linux 사용자: `heartbeat install-service` 한 줄로 등록 완료. enable-linger는 선택 (세션이 살아있는 동안만 돌면 충분하면 생략 OK).

## [0.4.0] - 2026-05-15

### Added

- 윈도우 호환. `pip install claude-heartbeat`이 macOS / Windows 양쪽에서 동작한다 (Linux systemd 통합은 Phase 3 예정).
- `heartbeat install-service` / `heartbeat uninstall-service` 명령. OS 감지하여 launchd plist (macOS) 또는 Task Scheduler 잡 (Windows)을 자동 등록 / 해제. `--print-only`로 실제 등록 없이 명령만 출력 가능.
- `dream-prep check-unprocessed --slug=...` 명령. 미처리 transcript가 있으면 exit 0, 없으면 exit 1. 셸-의존 0의 exit-code 게이트로 heartbeat condition에서 `grep` 같은 유닉스 도구 없이 동작한다.
- `[notify]` extras로 옵셔널 plyer 의존: `pip install claude-heartbeat[notify]`. 미설치 시 알림은 silently 로그로만 남는다.
- 테스트 39개 (32 → 39): _lock 동시성(ProcessPoolExecutor), check-unprocessed exit code, install-service print-only 모드, condition hotfix 회귀 방지.

### Changed

- 동시성 락: `fcntl` (POSIX 전용) → `portalocker` (cross-platform). meta.py의 락 코드는 `skills/dream/_lock.py`로 격리되어 한 파일 diff로 교체.
- 프로세스 트리 종료: `os.killpg` + `signal` → `psutil.Process.children()` 기반 cross-platform 트리 walk + terminate/kill. macOS / Windows 일관 동작.
- subprocess.Popen process group 분리: `start_new_session=True` (POSIX) / `creationflags=CREATE_NEW_PROCESS_GROUP` (Windows)을 OS별 분기.
- `heartbeat start`는 항상 foreground 동작. 데몬 detach (`os.fork` / `os.setsid`)는 제거. 백그라운드 실행은 OS 스케줄러(launchd / Task Scheduler)에 위임. `--foreground` 플래그는 호환을 위해 인자만 유지하고 동작 noop.
- notification: macOS osascript subprocess 호출 → plyer (cross-platform).
- dream skill heartbeat.md 템플릿의 condition을 `dream-prep check-unprocessed --slug={slug}`로 교체 (B안). 기존 HEARTBEAT.md의 셸 condition은 그대로 동작 (사용자 책임).
- CI matrix가 ubuntu / macOS / Windows × Python 3.11 / 3.12를 모두 머지 게이트로 검사.

### Migration

- 기존 launchd plist 사용자: 변경 불필요. 이미 `heartbeat start --foreground`를 호출하던 plist는 그대로 동작 (start가 foreground로 통일됐고 `--foreground` 인자도 noop으로 유지됨).
- 기존 HEARTBEAT.md의 dream condition: 그대로 동작. 새 install부터만 `check-unprocessed` 명령을 사용. 기존 잡을 갱신하려면 HEARTBEAT.md를 직접 편집하거나 `heartbeat install dream`을 다시 실행.
- 기존 `heartbeat start`(detach 모드)를 터미널에서 직접 호출하던 사용자: 이제 foreground로 돌므로 `nohup heartbeat start &` 또는 `heartbeat install-service`로 OS 스케줄러에 위임 권장.
- 신규 의존성: `portalocker`, `psutil`. `pip install -U claude-heartbeat`로 자동 설치.

## [0.3.0] - 2026-05-15

### Changed

- dream-prep: 단일 1240줄 `preprocess.py`를 책임 단위 4모듈로 분리. 외부 동작 변화 없음.
  - `skills/dream/paths.py`: 공통 PROJECTS_DIR / get_project_dir
  - `skills/dream/meta.py`: dream_meta.md 파싱·마킹·GC·fcntl lock
  - `skills/dream/window.py`: classify_transcript / compute_round_window / find_unprocessed
  - `skills/dream/extract.py`: 대화 추출·코드 압축·도구 호출 합치기·라운드 캡
  - `skills/dream/cli.py`: argparse + main + preprocess_project
- pyproject: `dream-prep` entry point를 `skills.dream.preprocess:main` → `skills.dream.cli:main`으로 갱신.

### Added

- `tests/`: pytest 기반 단위 테스트 32개. v0.2.1 hotfix(자동 초기화 / GC / cursor hard fail / huge bypass) 회귀 방지 포함.
- `pip install -e .[dev]`로 pytest + pytest-cov 설치 가능.
- `.github/workflows/ci.yml`: ubuntu / macOS × Python 3.11 / 3.12 매트릭스 CI. windows는 Phase 2(POSIX 의존 제거) 전까지 `continue-on-error` 자리만 잡아둠.

### Migration

- 호환성 변경 없음. `dream-prep` / `heartbeat` CLI 시그니처, HEARTBEAT.md, dream_meta.md 모두 그대로 동작.
- `from skills.dream.preprocess import ...`로 직접 import 하던 외부 코드가 있다면 모듈명 변경 필요 (없을 것으로 가정하고 shim 안 둠).

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
