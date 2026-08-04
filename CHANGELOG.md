# Changelog

이 프로젝트의 주요 변경사항을 기록한다. 형식은 [Keep a Changelog](https://keepachangelog.com/) 1.1.0을 따른다.

## [Unreleased] — 0.8.0 후보

2026-08-04 도그푸딩에서 실측된 사고 두 건(프로젝트 간 잡 증발, 장기 세션의 전역 틱 블로킹)의
구조적 해결. 배경은 `docs/planning/2026-08-04-productization.md`.

### Added

- 잡별 `model` 필드 — `claude --model <값>`으로 전달. 비어 있으면 CLI 기본 모델.
  (이전 세션 WIP를 이번 릴리스에 포함)
- `~/.claude/heartbeat/jobs.d/<slug>.md` — 프로젝트당 잡 파일 하나 (P0-A). 외부 도구는 자기
  slug 파일만 통째로 쓰면 되고, 마커 블록·부분 병합이 계약에서 사라진다. 병합 우선순위와 잡
  문법은 `docs/config-contract.md`에 고정.
- `heartbeat migrate` — HEARTBEAT.md의 잡을 slug별 jobs.d 파일로 분리. slug 없는 블록·전역
  설정·HTML 주석(외부 도구 마커)은 짝이 맞은 채 원본에 남고, 원본은 실행 전 백업. `--dry-run` 지원.
- condition 스킵 사유 통로 — condition stdout 첫 줄(최대 200자)을 `state.json` 잡 항목의
  `last_condition_output`에 저장. 소비자 화면이 "왜 건너뛰었는지"를 보여줄 수 있게 된다.
  사유를 내는 것은 조건 스크립트 소유자의 선택이고 데몬은 통로만 보장한다.

### Changed

- 스케줄러 사이클 배리어 제거 (P0-B). 이전에는 매 tick마다 전 slug 그룹의 완료를 기다려 한
  프로젝트의 장기 세션이 모든 프로젝트의 다음 tick을 세웠다. 이제 due 그룹을 상주 executor에
  제출만 하고 바로 다음 tick으로 넘어가며, 실행 중인 그룹은 due여도 건너뛴다(in-flight 가드).
  같은 slug 순차 실행과 데몬 안 같은 잡 중복 실행 금지는 그대로다.
- `_check_condition`이 `(bool, 사유)` 튜플을 반환한다 (내부 API).
- due 판정 시각을 디스패치 시각 고정에서 잡별 그 시점 시각으로 변경. 그룹 앞 잡이 오래 돌면
  뒤 잡의 판정이 한 라운드 밀리던 문제 해소.
- `heartbeat init`이 jobs.d 디렉토리도 만든다.

### Fixed

- condition을 프로젝트 cwd에서 실행. 이전에는 launchd 데몬의 cwd 기준이라 상대 경로
  condition이 전부 깨졌다. (이전 세션 WIP를 이번 릴리스에 포함)
- `state.json` 쓰기를 원자적 교체(temp + rename)로 변경. 소비자 앱이 폴링 중 잘린 JSON을
  읽고 그 틱 데이터를 조용히 버리던 문제.
- CWD 존재 확인을 condition 검사 앞으로 이동. 삭제된 프로젝트에서 "condition 실행 실패"가
  진짜 원인(CWD 없음)을 가리던 문제.
- quota 판정(`recent_runs` 정리·변이)을 상태 락 안으로 이동. 배리어 제거로 동시 그룹이 늘며
  직렬화 중인 상태와 경합할 수 있던 문제.
- jobs.d에서 일반 `.md` 파일만 읽는다(`.md` 이름의 디렉토리 등 무시).
- 데몬 실행 중 `heartbeat once`를 돌리면 같은 잡이 겹칠 수 있음을 CLI가 경고한다.

### Migration

- 100% 하위호환. 기존 HEARTBEAT.md 그대로 동작하고, jobs.d는 옵트인이다.
- 분리를 원하면 `heartbeat migrate` 한 번. 데몬 재시작 후 적용된다.

## [0.7.0] - 2026-05-15

### Added

- HEARTBEAT.md에 `max_per` 필드 추가. 슬라이딩 윈도우 quota — `max_per: 5/24h` = "지난 24시간 안에 최대 5번 실행". 윈도우 가득 차면 그 다음 요청은 `last_result: quota_skipped`로 자동 skip. 자정 리셋 안 씀(timezone 의존 0).
- `heartbeat-register` 스킬 신규. 자연어 한 줄로 잡 등록:
  - 단순 명령(예: "git log 요약") → prompt 직접 박음
  - 복잡 multi-step(예: "웹 리서치 + 디스코드 포스트") → 사용자 동의 후 새 SKILL.md 생성 + 잡과 페어로 등록
  - quota 자연어 표현("하루 N번", "주 M번") 인식 → `max_per` 자동 박음
  - `~/.claude/skills/` 또는 `~/.claude/HEARTBEAT.md`에 사용자 명시적 동의 없이 변경 금지 — 모르는 스킬이 갑자기 늘어나는 일 차단
- 회귀 테스트 13종: max_per 파싱(정상/위반/단위 fallback), 잡 파싱, 윈도우 정리, run_job quota skip, claude 호출 시 timestamp 기록, max_per 없는 잡 영향 0, 윈도우 슬라이드.

### Changed

- `parse_heartbeat_md`: 잡 dict에 `max_per` 키 추가 (None 또는 (count, window_sec) 튜플).
- `run_job`: condition 체크 직전에 quota 체크. claude 호출 시점에 `state["recent_runs"]`에 timestamp append (success/failure/timeout 모두 포함 — 토큰은 이미 소비됐으므로).
- `state.json` 스키마에 `recent_runs` 키 추가 (잡별, 옵션). 기존 잡은 max_per가 None이라 안 만들어짐.

### Migration

- 100% 하위호환. 기존 HEARTBEAT.md / state.json 그대로 동작.
- `pip install -U claude-heartbeat` 한 줄로 업그레이드.
- quota를 쓰려면 잡 블록에 `- max_per: N/Mh` 한 줄 추가하거나 `heartbeat install heartbeat-register` 후 자연어로 등록.

## [0.6.0] - 2026-05-15

### Changed (정책 — 동작 변화 있음)

- `_check_condition()`이 timeout / 예외 시 `False` 반환 (fail-closed). 이전엔 `True` 반환(fail-open)이라 dream-prep CLI가 깨지거나 condition이 hang되면 매 tick마다 claude를 깨워 토큰 비용 누적. zero-cost gating 약속과 모순됐던 디폴트 정정 (issue #10). 명시적으로 fail-open이 필요한 잡은 condition에 `|| true`를 박는다.
- `_acquire_meta_lock()`이 `LOCK_TIMEOUT_SEC` 안에 락을 잡지 못하면 `LockTimeout` 예외 raise (fail-closed). 이전엔 warning 후 lock 없이 yield해서 dream_meta.md cursor state의 race로 인한 중복 흡수 / 라운드 윈도우 누락 위험 (issue #11). `mark_processed`의 try/except가 LockTimeout을 잡아서 logger.warning + return — main flow는 안 죽지만 entry는 안 박힘 (race보다 안전).

### Added

- 회귀 테스트 8종: condition timeout / 예외 / 빈 condition / exit 0 / exit non-zero (5), lock 타임아웃 raise / mark_processed graceful 처리 / 정상 경로 (3).
- `LockTimeout` 예외 클래스 (`skills.dream._lock`).

### Documentation

- README의 "prompt accepts anything" 문구를 "any single-line value ... 긴 프롬프트는 skill로"로 정정 (issue #12). 멀티라인 prompt 자체 지원은 백로그 (skill 패턴이 더 정석).

### Migration

- 동작 변화 두 가지 — 기존 사용자에게 영향 가능:
  1. condition이 일시적으로 깨지던 잡은 이제 silent skip된다 (이전엔 그래도 claude 깨움). 의도가 "에러여도 돌리자"였다면 condition 명령에 `|| true` 추가.
  2. `dream-prep prep`을 두 인스턴스 동시에 돌리면 한쪽이 LockTimeout으로 실패할 수 있다. 정상적인 단일 heartbeat 데몬 사용 패턴에선 영향 없음.
- pyproject 버전: 0.5.x → 0.6.0 (정책 변경이라 minor bump).

## [0.5.2] - 2026-05-15

### Fixed

- `find_unprocessed_transcripts()`가 v2 `status: active` 마킹된 파일을 영영 스킵하던 회귀 (issue #9). 이전 구현은 `get_combined_processed()`로 legacy + v2 모든 파일명을 union해서 active도 "처리됨"으로 간주했다. 결과: 라운드 윈도우 동결 후 Claude가 transcript에 더 append해도 다음 라운드에서 잡히지 않음 → cursor 이어쓰기 자체가 망가짐.

### Changed

- `find_unprocessed_transcripts()` 판정 로직: legacy entry는 sealed로, v2 entry는 `status` 필드 분기 (sealed면 skip / active면 classify gate가 통과시키면 잡힘). 신규 파일은 그대로 gate만 통과하면 잡힘.

### Added

- 회귀 테스트 5종: 신규 파일 / legacy 마킹 / v2 sealed / v2 active huge(핵심 회귀) / active 작은 파일(gate 차단).

### Migration

- 호환성 변경 없음. dream_meta.md 형식 그대로. 기존 active 마킹된 파일들이 다음 dream 사이클부터 다시 잡혀서 cursor 이어쓰기가 정상 동작.

## [0.5.1] - 2026-05-15

### Fixed

- `heartbeat install dream` / `heartbeat skills`가 일반 `pip install claude-heartbeat`(wheel install)에서 "사용 가능한 스킬 없음"으로 실패하던 회귀 (issue #7). `_get_package_skills_dir`의 fallback이 `site-packages/heartbeat/skills`를 가리켰는데, pyproject.toml의 `packages = ["src/heartbeat", "skills"]`로 인해 실제 설치 위치는 top-level `site-packages/skills/`. `import skills; Path(skills.__file__).parent`로 통일해서 editable / wheel / zipapp 모두 동작.

### Added

- CI에 `wheel-install-smoke` 잡 추가. wheel build → 격리 venv install → `heartbeat skills`가 `dream`을 디스커버리하는지 검증. editable install에선 안 잡히던 패키징 회귀를 영구 차단.
- 단위 테스트: `_get_package_skills_dir` / `_list_available_skills`의 sanity 회귀 방지.

### Migration

- 호환성 변경 없음. 0.5.0 wheel install이 깨졌던 사용자만 0.5.1로 업그레이드하면 됨.

## [0.5.0] - 2026-05-15

### Added

- Linux systemd 통합. `heartbeat install-service`가 Linux에서 `~/.config/systemd/user/claude-heartbeat.service` user unit을 자동 생성하고 `systemctl --user daemon-reload && systemctl --user enable --now claude-heartbeat.service`까지 실행한다. 로그아웃 후에도 돌리려면 `loginctl enable-linger $USER`를 별도 안내.
- `heartbeat uninstall-service`가 Linux에서 `disable --now` + unit 파일 삭제 + `daemon-reload`까지 처리. 환경 이전(systemctl 부재 + unit 파일만 잔존) 케이스도 정리.
- `--print-only` 모드에서 systemd unit 내용 + install/enable 명령 + 자가검증용 `systemctl --user status` + linger 안내 + DBUS/XDG_RUNTIME_DIR 주의 문구까지 출력. 실제 부수효과 0.
- `heartbeat uninstall-service`도 `--print-only` 지원 (어댑터 인터페이스 비대칭 해소).
- 어댑터별 회귀 테스트 5종: systemctl 부재 graceful fail / 부분 실패(daemon-reload OK / enable FAIL) 시 unit 파일 보존 + 재시도 안내 / print-only 부수효과 0 / uninstall 환경 이전 / unit 파일 write 권한 실패.

### Changed

- `service.py`(317줄) → `service/` 패키지로 분리: `base.py` (ServiceAdapter 추상) / `launchd.py` / `task_scheduler.py` / `systemd.py` / `__init__.py` (디스패처).
- 어댑터 클래스화 — `ServiceAdapter`를 상속해 `render` / `install` / `uninstall`만 구현. 새 어댑터 추가는 `ADAPTERS` dict 한 줄 등록 (linux는 startswith라 별도 분기).
- `install_service` / `uninstall_service` 디스패처가 `sys.platform` 분기 1곳으로 통일. 이전엔 install / uninstall 함수에 분기가 두 군데 복붙됐음.
- 어댑터 인터페이스 일관성: `_install_task_scheduler`에 `render` 분리, `_uninstall_*`에 `print_only` 추가, 모든 어댑터가 동일 시그니처.
- systemd 어댑터: `daemon-reload`와 `enable --now`를 분리 실행. enable에서만 실패하면 unit 파일을 보존하고 "enable만 다시 시도" 안내 + 가능 원인(SSH 세션의 DBUS_SESSION_BUS_ADDRESS / XDG_RUNTIME_DIR 미설정) 한 줄.
- `cli.py`의 install-service / uninstall-service 지연 import 제거 → top-level (이득 0이었음).
- README Prerequisites: "macOS / Windows / Linux".

### Migration

- 호환성 변경 없음. CLI 시그니처 / HEARTBEAT.md / 기존 launchd plist 모두 그대로 동작.
- Linux 사용자: `heartbeat install-service` 한 줄로 등록 완료. enable-linger는 선택.
- `from heartbeat.service import install_service, uninstall_service`는 동일 (패키지 분리는 internal).

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
