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
```

`contract`는 입력 없이 지원 API 버전, 런타임 버전, 역할, provider, 실행 방식, 기본 상한과 아직
예약만 된 명령을 반환한다. 이 릴리스는 계약 조회, 설정 검증·저장·조회, 빈 상태 조회만 구현한다.
실행 계획, 시작, 일시 정지·재개, 취소·재시도, 로그, provider 진단은 계약 이름만 예약되어 있고
후속 릴리스에서 구현한다.

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

상태 응답은 해당 프로젝트의 설정, 큐, 실행, 오류만 반환한다. 이 작업에서는 실행기를 시작하지 않으므로
새 프로젝트의 큐·실행·오류 배열은 비어 있다. 이 분리는 후속 dispatcher가 다른 프로젝트의 상태를
섞지 않고 기록할 저장 경계도 함께 정한다.

실행 상태 이름은 reserved, queued, running, paused, succeeded, failed, cancelled,
recovery_required다. 앱은 SQLite 행이 아니라 `state` 응답의 이 이름을 사용한다.

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
