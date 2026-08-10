# 에이전트 런타임 계약

앱은 새 프로젝트별 에이전트 기능을 위해 Heartbeat 내부 파일, SQLite 테이블, 운영체제 서비스 파일을
읽지 않는다. 이 문서의 `heartbeat agent` JSON 계약만 사용한다. 기존 잡과 `state.json` 계약은
`config-contract.md`에 남으며 이 계약의 일부가 아니다.

## 전송 규칙

- 요청은 표준 입력의 JSON 객체 하나이고 응답은 표준 출력의 JSON 객체 한 줄이다. 진단은 표준 오류로
  보낸다.
- 모든 요청에는 문자열 `apiVersion: "1"`과 비어 있지 않은 문자열 `requestId`가 필요하다.
- 응답은 `apiVersion`, `runtimeVersion`, `requestId`, `command`, `outcome`을 항상 포함한다.
- `outcome`은 `success`, `partial_success`, `failure` 중 하나다. 실패에는 `error.stage`와
  `error.code`가 있으며, 설정을 거절하는 실패 단계는 `request_validation`이다.
- 실패 단계의 전체 목록은 `request_validation`, `reservation`, `provider_start`, `role_session`,
  `cleanup`, `recovery`다. 실행 기능이 추가되면 이 목록으로 부분 실패의 위치를 구분한다.
- 같은 주 버전 응답의 모르는 선택 필드는 소비자가 무시한다. 지원하지 않는 주 버전은 쓰기 전에
  `unsupported_api_version`으로 거절한다.

## 지원 명령

```text
heartbeat agent contract
heartbeat agent config validate
heartbeat agent config write
heartbeat agent config read
heartbeat agent state
heartbeat agent plan
heartbeat agent run start
heartbeat agent run cancel
heartbeat agent run retry
heartbeat agent project pause
heartbeat agent project resume
heartbeat agent logs
heartbeat agent provider diagnose
```

`contract`는 입력 없이 지원 API 버전, 런타임 버전, 역할, provider, 실행 방식, 기본 상한과 구현된
명령 목록을 반환한다. 실행 명령 여덟 개(`plan.read`, `run.start`, `project.pause`,
`project.resume`, `run.cancel`, `run.retry`, `logs.read`, `provider.diagnose`)가 구현됐으므로
`reservedCommands`는 빈 목록이다.

## 설정 요청

설정 검증과 저장은 아래 요청을 쓴다. 검증은 상태를 쓰지 않는다. 저장은 프로젝트 하나의 완전한 설정과
`deviceMaxParallel` 기기 전역값 하나를 같은 트랜잭션으로 교체한다. 프로젝트마다 이 값을 따로 소유하지
않는다.

```json
{
  "apiVersion": "1",
  "requestId": "request-123",
  "configuration": {
    "projectId": "prj_123",
    "workingDirectory": "/absolute/project/path",
    "projectMaxParallel": 3,
    "deviceMaxParallel": 16,
    "paused": false,
    "eventRetentionDays": 30,
    "roles": {
      "planner": {"provider": "claude", "model": null, "executionMode": "once", "maxParallel": 1, "pollIntervalSeconds": 300, "executionLimit": null},
      "architect": {"provider": "codex", "model": "gpt-5.6", "executionMode": "continuous", "maxParallel": 1, "pollIntervalSeconds": 300, "executionLimit": null},
      "developer": {"provider": "claude", "model": null, "executionMode": "once", "maxParallel": 1, "pollIntervalSeconds": 300, "executionLimit": null}
    }
  }
}
```

`projectId`와 실제 디렉터리는 필수다. slug만으로 경로를 추측하지 않으며, 존재하지 않는 디렉터리는
저장 전에 거절한다. 역할은 planner, architect, developer를 각각 한 번씩 포함한다. provider는
claude 또는 codex이고 실행 방식은 once 또는 continuous다. 역할 기본 상한은 1, 프로젝트 기본 상한은
3이다. 기기 값 16은 구형 소비자를 위한 계약 fallback일 뿐 하드 상한이 아니다. 새 소비자는
`deviceCapacity.recommendedMaxParallel`을 첫 설정으로 쓰며 사용자는 그보다 낮거나 높은 양의 정수를
저장할 수 있다. 프로젝트 상한도 기기 전역값보다 높게 둘 수 있고 실제 배정은 둘 중 작은 쪽을 따른다.
역할 상한만 프로젝트 상한을 넘을 수 없다. 실행 이벤트와 오류 메타데이터의 기본 보존 기간은 30일이며,
후속 실행기가 이 정책에 따라 오래된 기록을 정리한다.

설정 객체에는 계약에 없는 필드를 허용하지 않는다. 따라서 prompt 원문, 인증 토큰, API 키, 전체 환경
변수는 이 저장소에 들어갈 수 없다.

## 조회 요청과 상태

설정과 상태 조회는 다음처럼 프로젝트 식별자만 받는다.

```json
{"apiVersion":"1","requestId":"request-124","projectId":"prj_123"}
```

`config.read`와 저장 응답은 설정과 함께 `deviceCapacity`를 싣는다. 이 값은 논리 CPU 수와 전체 메모리,
OS와 앱을 위해 남긴 메모리, 에이전트 프로세스 트리당 1.5 GiB 추정치, 사양 기반 권장값, 사용자가 저장한
전역값, 실제 적용값, 현재 활성 실행 수와 프로젝트별 상한·활성 실행 수를 포함한다. 권장값은 논리 CPU
하나를 남긴 수와 메모리 허용량 중 작은 값이며 **실행을 막는 상한이 아니다**. 사용자가 저장한 값이 없을
때만 권장값이 실제 적용값이 된다. 프로젝트별 상한 합은 예약량이 아니며 프로젝트들이 현재 빈 기기 슬롯을
공유한다.

상태 응답은 해당 프로젝트의 설정, 큐, 실행, 오류만 반환한다. 아직 아무것도 시작하지 않은 프로젝트의
큐·실행·오류 배열은 비어 있다. 큐에는 아직 쓰이지 않은 계획과 반복 실행이 이어갈 지시가 들어가고,
실행에는 예약·시작·복구를 거친 행이 들어간다. 이 분리가 dispatcher의 저장 경계이므로 다른 프로젝트의
상태는 섞이지 않는다.

실행 상태 이름은 reserved, queued, running, paused, succeeded, failed, cancelled,
recovery_required다. 앱은 SQLite 행이 아니라 `state` 응답의 이 이름을 사용한다.

## 실행 명령

### 계획과 시작

`plan.read`는 역할별 슬롯 수와 선택적 대상 목록을 받아 계획 하나를 만든다. 프로세스를 만들지 않고
예약도 하지 않는다.

```json
{"apiVersion":"1","requestId":"request-200","projectId":"prj_123",
 "roles":{"developer":{"slots":2,"targets":["TASK-12"]}}}
```

응답의 `plan`은 일회용 `planId`, 런타임 개정 값 `revision`, 만료 시각 `expiresAt`, 기기·프로젝트
남은 슬롯, 적용된 상한, provider 진단, 과금 경로 위험, 역할별 `granted`와 `excluded` 사유를 담는다.
`granted`는 역할 상한·프로젝트 남은 슬롯·기기 남은 슬롯·provider 준비 상태·검증을 통과한 수동 대상
수의 최솟값이다. 실제로 예약 가능한 대상 수는 예약 도구만 알기 때문에 시작 단계에서 더 줄어들 수 있다.

`run.start`는 같은 `planId`와 `"confirmed": true`를 함께 받는다. 계획이 없거나 만료됐거나 런타임
개정 값이 달라졌으면 프로세스를 하나도 만들지 않고 `plan_not_found`, `plan_expired`,
`runtime_changed` 중 하나로 실패한다. 계획은 한 번만 쓰이므로 같은 `planId`로 다시 부르면
`plan_not_found`가 된다. 일부만 시작하면 `partial_success`로 응답한다.

### 예약 도구와 lease

시작은 프로젝트의 `.workflow/rules/wf-reserve.sh`(Windows는 `.ps1`)를 슬롯마다 한 번 호출하고
종료 코드로만 판단한다. 0은 JSON 한 줄, 1은 예약 없음, 2는 인자 오류다. 1은 실패 단계 `reservation`,
2는 `request_validation`으로 남기고 2는 재시도하지 않는다. 도구가 설치돼 있지 않으면 호출하지 않고
`reservation` 단계로 남기며, 런타임은 어떤 경우에도 lease 파일을 직접 만들거나 지우지 않는다.

예약 응답의 `rolePrompt`는 provider 표준 입력으로만 전달한다. 실행 행, 이벤트 파일, 일반 로그에
저장하지 않으며 런타임이 조립하거나 수정하지도 않는다. 예약 도구는 대상을 스스로 고르므로 수동 요청이
지정한 대상과 다른 대상이 예약되면 그 lease를 즉시 반납하고 슬롯을 거절한다.

lease 갱신과 반납은 `wf-claim.sh`를 직접 호출한다. 실행 중 갱신이 5를 내면 역할 세션이 인계받아
반납한 것으로 보고 갱신만 멈춘다. 프로세스는 종료하지 않는다. 종료 뒤 반납의 0과 5는 모두 정리
성공이고 1만 정리 실패로 남는다.

### 취소·재시도·로그·진단

`run.cancel`은 `confirmed`가 없으면 대상·PID·자식 프로세스 수와 정리 단계를 보여주는 미리보기만
반환한다. `"confirmed": true`면 프로세스 트리를 멈추고 lease를 반납한 뒤 단계별 결과를 반환한다.
프로세스 신원이 다르거나 확인되지 않으면 그 PID를 종료하지 않는다. 일부 단계가 실패하면
`partial_success`와 남은 단계를 반환하고 실행은 `recovery_required`가 된다.

`run.retry`는 이전 실행 식별자를 필수로 받고 그 행을 그대로 둔 채 새 예약과 새 실행 식별자를 만든다.
새 실행 행은 `previousRunId`로 이전 실패를 가리킨다.

`logs.read`는 실행 식별자와 `cursor`만 받아 민감정보가 제거된 이벤트 묶음과 `nextCursor`를 반환한다.
화면에서 받은 파일 경로는 쓰지 않는다.

`provider.diagnose`는 그 프로젝트가 설정한 provider별 준비 상태를 반환한다.

### 복구

실행 행은 실행 식별자, 프로젝트, 역할, provider, 예약 대상, `leaseId`, `resultPrefix`, 예약 만료
시각, PID, 시작 시각, 프로세스 생성 신원, 이벤트 파일 경로, 마지막으로 읽은 offset, 이전 실행
식별자를 담는다. 런타임이 다시 시작하면 저장된 PID와 생성 신원을 대조해 같은 프로세스만 이어서
감시하고, 이벤트는 마지막 offset부터 다시 읽는다. PID가 같아도 생성 신원이 다르거나 확인할 수 없으면
실행 중으로 추측하지 않고 `recovery_required`로 남긴다. 프로세스가 이미 끝났으면 이벤트 파일의 마지막
이벤트로 종료 상태를 정하고 lease를 반납한다.

## 저장소와 호환성

런타임은 사용자 홈의 Heartbeat 저장 영역에서 SQLite 데이터베이스와 스키마 버전을 소유한다. WAL과
짧은 쓰기 트랜잭션을 사용해 데몬과 제어 CLI의 동시 쓰기를 직렬화한다. 앱은 이 파일이나 테이블을
직접 열거나 마이그레이션하지 않는다. 기존 `jobs.d`, Heartbeat 잡, Dream 설정과 기존 `state.json`은
새 에이전트 상태의 원천이 아니며 변경하지 않는다.

## 독립 실행형 배포와 서비스 복구

런타임 릴리스는 macOS universal, Linux x86_64, Windows x86_64용 one-folder 배포물이다.
각 배포물은 `runtime-manifest.json`으로 target, runtime version, API major, 모든 파일 해시를
검증한다. `heartbeat runtime inspect`는 아래 기기 상태를 JSON으로 내고,
`heartbeat runtime verify-manifest --root <directory>`는 해시와 API major가 맞을 때만
성공한다.

설치기는 검증된 배포물을 `versions/<version>`에 둔 뒤 stable launcher 하나만 원자적으로
바꾼다. `heartbeat runtime activate --install-root <root> --version-dir <version-dir>`는
검증을 먼저 하므로 실패한 새 설치가 실행 중인 버전을 훼손하지 않는다. 서비스 정의는 stable
launcher를 실행해야 하며, `HEARTBEAT_RUNTIME_LAUNCHER`가 있으면 이를 우선 사용한다.

### 기기 상태 조회

`heartbeat runtime inspect [--install-root <root>]`는 설치 상태와 서비스 상태를 한 JSON 응답으로
돌려준다. 같은 사실을 돌려주는 두 번째 명령은 만들지 않는다.

```json
{
  "schemaVersion": 1, "result": "ok", "checkedAt": "2026-08-08T08:41:23Z",
  "runtimeVersion": "0.8.0", "installedVersion": "0.8.0", "runningVersion": "0.8.0",
  "apiMajor": 1, "target": "macos-universal", "executable": "/…/heartbeat",
  "installRoot": "/…/install", "launcher": "/…/install/bin/heartbeat",
  "installResult": "installed", "recoverable": true,
  "service": {
    "platform": "launchd", "result": "registered", "registered": true, "running": true,
    "label": "com.claude-heartbeat", "executable": "/…/install/bin/heartbeat",
    "recoverable": true, "checkedAt": "2026-08-08T08:41:23Z",
    "evidence": ["launch_agents_directory", "program_arguments", "launchctl_list"],
    "detail": {}
  },
  "evidence": ["stable_launcher", "version_manifest", "launch_agents_directory"]
}
```

- `runtimeVersion`은 이 호출에 답한 실행 파일의 버전, `installedVersion`은 stable launcher가 가리키는
  버전 디렉터리의 manifest 값이다. `runningVersion`은 서비스가 실행 중임이 확인됐고 그 등록물의 실행
  경로에서 버전을 읽어낼 수 있을 때만 채워진다. 셋은 서로 다른 사실이며 하나로 나머지를 추측하지 않는다.
- `installResult`는 `installed`, `launcher_missing`, `version_missing`, `manifest_unreadable`,
  `unsupported_version` 중 하나다. 최상위 `result`는 `ok`, `unsupported_platform`,
  `unsupported_version` 중 하나다.
- `service.result`는 `registered`, `not_registered`, `executable_missing`, `ambiguous_registration`,
  `permission_denied`, `tool_missing`, `unsupported_platform` 중 하나이며 세 운영체제가 같은 값을 같은
  뜻으로 쓴다. `registered`와 `running`은 참·거짓·null 세 값이고 null은 확인하지 못했다는 뜻이다.
  확인하지 못한 값과 권한 때문에 확인할 수 없는 값은 어떤 경우에도 실행 중으로 올라가지 않는다.
- `recoverable`은 등록이 확인됐고 그 실행 파일이 있을 때만 참이다. 등록물이 여러 개라 무엇을 재기동할지
  정할 수 없으면 거짓이고, 확인 자체를 못 했으면 null이다.
- `evidence`는 판정이 무엇을 읽어서 나왔는지 남긴다. `detail`은 플랫폼 고유 이름과 명령 출력만 담는 선택
  항목이며 계약 필드가 아니다. Windows는 Task Scheduler의 상태 출력이 로케일에 따라 달라져 `running`을
  null로 두고 그 사유를 `detail`에 적는다.
- 조회는 읽기 전용이다. 호출 전후로 런타임 파일, launcher, 서비스 정의, SQLite와 프로젝트 설정이 바뀌지
  않는다.

macOS LaunchAgent는 KeepAlive와 RunAtLoad를, Linux systemd user unit은 Restart=always를,
Windows Task Scheduler는 RestartOnFailure와 중복 실행 방지 정책을 사용한다. Linux user
service는 로그아웃 이후에도 실행하려면 `loginctl enable-linger`가 필요하다. 서비스가 재시작돼도
runtime SQLite와 기존 state 파일이 남으므로 다음 dispatcher는 영속 상태를 먼저 복구하고 같은
작업을 중복 시작하지 않아야 한다.

## 릴리스 검증 계약

런타임 CI와 릴리스는 `tests/test_agent_end_to_end.py`를 macOS, Linux, Windows에서 같은 fixture로
실행한다. fixture는 로컬 Python 프로세스를 Claude와 Codex CLI 대신 사용하므로 provider SDK, 실제
계정, API 키와 유료 요청이 없다. 검사는 다음 경계를 한 묶음으로 고정한다.

- 계약의 provider 목록은 Claude와 Codex만 포함하고 Dream을 포함하지 않는다.
- 두 프로젝트의 설정, 반복 의도, 실행 상태가 SQLite 재접속 뒤에도 서로 섞이지 않는다.
- 역할, 프로젝트, 기기 상한과 예약 가능한 대상 수 중 가장 작은 값만 실제 실행으로 이어진다.
- 대상 없음과 migration lock에서는 provider 생성 함수를 호출하지 않는다.
- 여러 dispatcher가 같은 대상을 요청해도 하나의 reservation과 provider 프로세스만 생긴다.
- 역할 prompt는 provider 표준 입력으로만 전달되고 SQLite에는 남지 않는다.

target별 one-folder 배포물은 게시 전에 manifest를 자체 검증하고 실제 `agent contract` 응답을 낸다.
앱 릴리스는 그 배포물을 다시 검증하므로, runtime version, API 주 버전, target, 파일 해시 또는 실제
계약 응답 중 하나라도 다르면 앱 bundle 전에 실패한다. release publish는 모든 target build가 성공한
뒤에만 실행한다.

플랫폼 서비스 smoke test는 실제 서비스 등록, 시작, 강제 종료, 자동 재시작, 해제를 한 순서로 수행한다.
새 PID와 이전부터 보존된 run ID를 비교해 프로세스 복구와 작업 중복 방지를 구분한다. 플랫폼의 서비스
관리 기능을 사용할 수 없으면 검사를 성공으로 건너뛰지 않고 환경 오류로 실패시킨다. 해제와 임시 파일
정리는 검사 성공 여부와 관계없이 실행한다.
