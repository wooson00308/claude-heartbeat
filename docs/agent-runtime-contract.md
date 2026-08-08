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

설정 검증과 저장은 아래 요청을 쓴다. 검증은 상태를 쓰지 않고, 저장은 프로젝트 하나의 완전한 설정을
원자적으로 교체한다.

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
3, 기기 기본 상한은 16이다. 기기 상한은 16보다 크게 저장할 수 없고 프로젝트·역할 상한은 그 상위
상한을 넘을 수 없다. 실행 이벤트와 오류 메타데이터의 기본 보존 기간은 30일이며, 후속 실행기가 이
정책에 따라 오래된 기록을 정리한다.

설정 객체에는 계약에 없는 필드를 허용하지 않는다. 따라서 prompt 원문, 인증 토큰, API 키, 전체 환경
변수는 이 저장소에 들어갈 수 없다.

## 조회 요청과 상태

설정과 상태 조회는 다음처럼 프로젝트 식별자만 받는다.

```json
{"apiVersion":"1","requestId":"request-124","projectId":"prj_123"}
```

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
검증한다. `heartbeat runtime inspect`는 설치한 실행 파일의 JSON 신원을 내고,
`heartbeat runtime verify-manifest --root <directory>`는 해시와 API major가 맞을 때만
성공한다.

설치기는 검증된 배포물을 `versions/<version>`에 둔 뒤 stable launcher 하나만 원자적으로
바꾼다. `heartbeat runtime activate --install-root <root> --version-dir <version-dir>`는
검증을 먼저 하므로 실패한 새 설치가 실행 중인 버전을 훼손하지 않는다. 서비스 정의는 stable
launcher를 실행해야 하며, `HEARTBEAT_RUNTIME_LAUNCHER`가 있으면 이를 우선 사용한다.

macOS LaunchAgent는 KeepAlive와 RunAtLoad를, Linux systemd user unit은 Restart=always를,
Windows Task Scheduler는 RestartOnFailure와 중복 실행 방지 정책을 사용한다. Linux user
service는 로그아웃 이후에도 실행하려면 `loginctl enable-linger`가 필요하다. 서비스가 재시작돼도
runtime SQLite와 기존 state 파일이 남으므로 다음 dispatcher는 영속 상태를 먼저 복구하고 같은
작업을 중복 시작하지 않아야 한다.
