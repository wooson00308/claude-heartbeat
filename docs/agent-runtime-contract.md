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

실행 권한 동의를 다루는 `consent.read`, `consent.grant`, `consent.revoke`도 구현된 명령 목록에 함께
실린다. 아래 "실행 권한 동의" 절이 이 세 명령의 요청과 응답을 정의한다.

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
    "automationEnabled": false,
    "paused": false,
    "eventRetentionDays": 30,
    "roles": {
      "planner": {"provider": "claude", "model": null, "executionMode": "once", "maxParallel": 1, "pollIntervalSeconds": 300, "executionLimit": null},
      "architect": {"provider": "codex", "model": "gpt-5.6-sol", "executionMode": "continuous", "maxParallel": 1, "pollIntervalSeconds": 300, "executionLimit": null},
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

## 실행 권한 동의

런타임은 프로젝트마다 사용자가 실행 권한 고지를 읽었다는 사실을 직접 보관한다. 앱이 닫혀도 배정이
이어지므로 이 기록은 앱이 아니라 런타임이 갖는다. 기록에 담기는 값은 프로젝트 식별자와 고지 버전과
동의 시각 셋뿐이며, 인증 정보나 작업 지시가 들어갈 자리는 없다. 동의 시각은 런타임이 스스로 읽은
시각이고 요청에서 받지 않는다.

`consent.read`와 `consent.revoke`는 프로젝트 식별자만 받고, `consent.grant`는 고지 버전을 함께 받는다.

```json
{"apiVersion":"1","requestId":"request-125","projectId":"prj_123","noticeVersion":1}
```

세 명령 모두 성공 응답에 같은 모양의 동의 객체 하나를 싣는다.

```json
{"consent":{"projectId":"prj_123","granted":true,"valid":true,"noticeVersion":1,"grantedAt":"2026-08-13T15:00:00Z","requiredNoticeVersion":1}}
```

`granted`는 기록이 있는지를, `valid`는 기록이 있고 그 고지 버전이 런타임이 요구하는 버전 이상인지를
뜻한다. 요구 버전은 `contract.read` 응답의 `requiredNoticeVersion`으로도 알린다. 요구 버전이 올라가면
이전 버전으로 남긴 동의는 `granted`가 참인 채 `valid`만 거짓이 되며, 기록 자체는 지워지지 않는다.

동의 기록이 없는 프로젝트의 `consent.read`는 실패가 아니라 성공이며, `granted`와 `valid`가 모두 거짓이고
고지 버전과 동의 시각이 비어 있다. `consent.revoke`는 지정한 프로젝트의 기록만 지우고 다른 프로젝트의
동의에 영향을 주지 않는다. 요구 버전보다 낮은 고지 버전으로 온 `consent.grant`는 기록을 남기지 않고
`consent_notice_outdated` 사유로 거절하며, 고지 버전이 정수가 아니면 다른 요청 검사와 같이
`invalid_request`로 거절한다. 세 명령은 저장된 프로젝트 설정을 요구하지 않는다. 사용자가 자동 배정을
처음 켜는 순간에는 아직 설정이 저장되기 전일 수 있기 때문이다.

요구 고지 버전은 고지 문구의 의미가 바뀌어 사용자가 다시 읽어야 할 때만 올린다. 맞춤법 교정, 문장
다듬기, 화면 배치 변경으로는 올리지 않는다. 올리면 그때까지의 모든 동의가 한꺼번에 무효가 되기
때문이다.

### 실행 경로의 동의 확인

새 실행을 시작하는 경로는 모두 시작 직전에 같은 판정 하나를 지난다. `granted`와 `valid`가 모두 참일
때만 다음 단계로 넘어가고, 그렇지 않으면 대상 예약과 provider 시작과 실행 한도 차감 없이 멈춘다.
동의를 남긴 적이 없는 프로젝트, 동의를 철회한 프로젝트, 요구 고지 버전이 올라 기존 동의가 무효가 된
프로젝트는 모두 같게 다룬다. 확인 지점은 경로마다 다음과 같다.

- 자동 배정: 프로젝트별 처리에서 실행 중 세션의 회복 절차를 마친 뒤, 역할별 배정 루프에 들어가기 전에
  판정한다. 그 프로젝트의 배정만 멈추므로 같은 기기의 다른 프로젝트는 영향을 받지 않는다.
- 직접 배정과 재시도: 시작 전 확인 함수의 맨 앞에서 판정한다. 큐에 넣었다가 나중에 시작하는 실행도
  같은 함수를 지나므로, 큐에 들어간 뒤 동의가 철회되면 그 실행은 예약을 잡지 않고 멈춘다.

동의가 없어 시작하지 못한 것은 실행 실패가 아니라 대기다. 실행 행과 오류 기록을 남기지 않고, 사유는
`execution_consent_required` 하나로 알린다. `run.start`는 그 역할을 `waiting`에 싣고 `failures`에는
아무것도 넣지 않으며, 응답 자체는 `success`다. `run.retry`는 `failure`로 답하되 오류의 `code`가
`execution_consent_required`이고 `stage`는 `reservation`이므로, 실행 도구 설치 실패나 로그인 필요,
권한 부족, 대상 없음과 구분된다.

이미 실행 중이던 세션은 이 판정의 대상이 아니다. 동의를 철회해도 그 세션은 그대로 이어지고 회복 절차도
평소처럼 수행된다. 판정이 막는 것은 새 실행의 시작뿐이다. `plan.read`는 예약도 시작도 하지 않으므로
이 확인의 대상이 아니며, 동의 전 계획 조회를 막는 일은 앱 화면이 맡는다.

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

상태 응답은 해당 프로젝트의 설정, 큐, 실행, 오류와 `automation`을 반환한다. 아직 아무것도 시작하지
않은 프로젝트의 큐·실행·오류 배열은 비어 있다. 큐에는 아직 쓰이지 않은 일회용 계획만 들어가고,
반복 의도는 `automation.roles`로 분리된다. 역할별 watching/waiting/running/attention 상태와 다음·마지막
확인 시각, watcher의 watching/degraded/stopped 상태, dispatcher 실행 여부를 이 객체에서 읽는다.
실행에는 예약·시작·복구를 실제로 거친 행만 들어간다. 대상 없음과 수동 대상 경합은 실행 행이나 오류를
만들지 않는다.

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

슬롯 계산은 활성 상태 넷에 더해 정리가 끝나지 않은 `recovery_required` 실행(점유 실행)을 함께 센다.
장부가 놓은 실행이라도 그 프로세스가 살아 일하고 있을 수 있으므로, 남은 정리 단계가 비기 전에는 그
자리를 새 실행에 주지 않는다. 계획은 `deviceOccupied`·`projectOccupied`로 점유 수를 싣고, 점유가
없었다면 더 배정될 역할의 `excluded`에는 평소의 `limit_reached` 대신 `slots_held_by_unrecovered_runs`가
실린다. 점유 실행 목록은 `revision`에 들어가므로 정리가 슬롯을 놓으면 이전 계획은 `runtime_changed`로
거부된다.

`run.start`는 같은 `planId`와 `"confirmed": true`를 함께 받는다. 계획이 없거나 만료됐거나 런타임
개정 값이 달라졌으면 프로세스를 하나도 만들지 않고 `plan_not_found`, `plan_expired`,
`runtime_changed` 중 하나로 실패한다. 계획은 한 번만 쓰이므로 같은 `planId`로 다시 부르면
`plan_not_found`가 된다. 일부만 시작하면 `partial_success`로 응답한다. 제어 명령은 각 실행을 먼저
`queued`로 저장하고 런타임 전용 감독 프로세스를 분리해서 띄운 뒤 응답한다. 감독 프로세스가 예약,
provider 시작, 이벤트 기록, lease 갱신과 종료 정리를 소유하므로 제어 명령이나 앱이 먼저 끝나도
provider의 표준 출력 파이프와 감시 수명이 끊기지 않는다.

`automationEnabled`를 켜고 역할을 `continuous`로 저장하면 런타임은 역할당 자동 배정 의도 하나를
저장하고 기기당 dispatcher 하나를 별도 프로세스로 띄운다. 직접 배정은 이 의도를 만들거나 바꾸지 않는다.
master를 끄면 새 자동 배정만 중단하고 역할 선택과 실행 중 세션은 유지한다. 역할을 `once`로 저장하면
그 역할의 자동 배정 의도만 제거한다.

dispatcher는 활성 프로젝트의 `.workflow`를 감시하고 파일 변경을 500ms debounce한 뒤 즉시 재판정한다.
watcher가 없거나 실패하면 역할의 `pollIntervalSeconds`를 안전 확인 주기로 사용하고 상태를 `degraded`로
낮춰 보고한다. 프로젝트 사이는 마지막 배정이 오래된 순서, 같은 프로젝트의 역할 사이는
`lastAssignedAt`이 오래된 순서로 빈 자리를 나눈다. 역할 내부 후보 순서는 `wf-eligible --json`이 정한
순서를 바꾸지 않으며 실제 실행 직전 `wf-reserve`가 다시 판정한다. 각 실행은 자기 감독 프로세스로
분리되므로 dispatcher가 종료돼도 이미 시작된 CLI와 lease 정리는 계속된다.

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

`kind`가 `tool`인 이벤트는 선택 필드 `toolName`에 도구 이름 하나를 싣는다. 값은 provider가 고정 어휘로
주는 이름이다. Claude는 도구 사용 블록의 이름을 그대로 쓰고, Codex는 MCP 도구 호출이면 그 도구 이름을,
이름을 담지 않는 항목이면 항목 유형(`command_execution` 등)을 쓴다. 이름을 확인할 수 없으면 값은
`null`이다. 도구에 넘긴 입력과 인자, 명령 문자열, 도구가 돌려준 결과 본문은 이 필드에도 다른 어떤
필드에도 싣지 않는다. `toolName`이 없거나 `null`인 기존 기록도 계약 위반이 아니므로 소비자는 그런 기록을
그대로 읽고 도구 이름이 없는 것으로만 다룬다.

`provider.diagnose`는 그 프로젝트가 설정한 provider별 준비 상태와 `modelCatalog`를 반환한다. Codex는
현재 로그인 계정이 목록에 공개한 모델만 `available`로 돌려준다. Claude는 CLI가 허용하는 공통 별칭을
`unverified`로 돌려주며, 계정·게이트웨이별 정식 모델 이름은 실행 전까지 존재 여부를 단정하지 않는다.
계획의 역할별 `diagnostic`은 `selectedModel`과 `modelStatus`를 함께 싣는다. Codex 카탈로그가 확인된
상태에서 선택 모델이 목록에 없으면 `model_unavailable`로 제외하고 예약·쿼터·provider 시작을 수행하지 않는다.

### 복구

실행 행은 실행 식별자, 프로젝트, 역할, provider, 예약 대상, `leaseId`, `resultPrefix`, 예약 만료
시각, PID, 시작 시각, 종료 시각, 프로세스 생성 신원, 감독 프로세스 신원, 이벤트 파일 경로, 마지막으로 읽은
offset, 이전 실행 식별자를 담는다. 상태 조회와 데몬 tick은 같은 신원의 감독 프로세스가 살아 있으면
그 실행의 이벤트나 lease를 대신 처리하지 않는다. 감독 프로세스가 사라진 예전 실행은 저장된 provider
PID와 생성 신원을 대조해 회수한다. 신원이 일치하는 살아 있는 provider는 감독 소유를 비운 활성 실행으로
되돌리고(재입양), 이후의 이벤트 옮기기·lease 갱신·종료 정리는 재조정 주체가 이어받는다. PID가 같아도
생성 신원이 다르거나 확인할 수 없으면 실행 중으로 추측하지 않고 `recovery_required`로 남긴다. provider
프로세스가 이미 끝났으면 이벤트 파일의 마지막 이벤트로 종료 상태를 정하고 lease를 반납한다.

남은 정리 단계가 있는 `recovery_required` 실행은 슬롯을 계속 점유하고, 데몬 tick이 매 주기 그 실행의
실제 상태를 다시 판정한다. 복구 단계가 놓은 실행 중 감독 프로세스가 저장된 신원 그대로 살아 있으면
실행을 감독자에게 그대로 되돌리고, 감독자 없이 provider가 신원 일치로 살아 있으면 감독 소유를 비운
활성 실행으로 되돌린다(재입양). 어느 쪽도 프로세스와 lease는 건드리지 않으며, 되돌아온 실행은 다시
활성으로 집계되고 화면의 앱 밖 세션 목록에서도 빠진다. 취소가 남긴 실행은 살아 있어도 되살리지 않고
점유로만 유지한다. 관찰할 수 없는 프로세스도 그대로 점유로 남긴다. 프로세스가 사라졌거나 PID가
재사용됐으면 남은 단계(이벤트 옮겨 닫기, lease 반납)를 수행하고, 모두 끝나면 이벤트 파일의 마지막
이벤트로 종료 상태를 정해 슬롯을 놓는다. 일부가 실패하면 실패한 단계만 남기고 점유를 유지한다.

`finishedAt`은 성공·실패·취소·복구 필요 상태로 전환한 시각이며 활성 실행에서는 비어 있다. 이 필드가
생기기 전의 종료 기록은 저장 행의 마지막 갱신 시각을 조회 응답에만 사용해 경과 시간을 고정하고, 과거
행 자체를 추정 값으로 다시 쓰지 않는다.

provider의 구조화 표준 출력만 실행 계약을 판정한다. 표준 오류의 일반 경고는 명시적인 완료 사건을
실패로 뒤집지 않는다. 프로세스가 0이 아닌 코드로 끝났을 때는 민감정보를 제거한 표준 오류를 종료 사유로
남기며, 내용이 없으면 종료 코드를 구조화된 사유로 남긴다.

## 저장소와 호환성

런타임은 사용자 홈의 Heartbeat 저장 영역에서 SQLite 데이터베이스와 스키마 버전을 소유한다. WAL과
짧은 쓰기 트랜잭션을 사용해 데몬과 제어 CLI의 동시 쓰기를 직렬화한다. 앱은 이 파일이나 테이블을
직접 열거나 마이그레이션하지 않는다. 기존 `jobs.d`, Heartbeat 잡, Dream 설정과 기존 `state.json`은
새 에이전트 상태의 원천이 아니며 변경하지 않는다.

기본 경로는 `~/.claude/heartbeat/agent-runtime.sqlite3`이다. schema v5는 프로젝트별 정책,
일회용 시작 계획, `agent_automation`의 역할별 자동 배정 의도·확인 시각·공정성 기준, watcher 상태,
실행 상태, 정규화된 진행 이벤트와 오류 단계, 현재 dispatcher의
PID·프로세스 신원만 저장한다. 프로젝트 문서의 내용, 역할 prompt 원문, provider 인증 토큰, API 키와
전체 환경 변수는 저장하지 않는다. SQLite가 필요한 이유는 앱이나 제어 명령이 종료된 뒤에도 반복 지시와
실행 사실을 잃지 않고, 재시작 뒤 살아 있는 프로세스를 같은 PID의 다른 프로세스와 구분하며, 여러 시작
요청이 같은 기기 슬롯과 dispatcher 소유권을 짧은 트랜잭션으로 정리하게 하기 위해서다.

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
  "runtimeVersion": "0.9.0", "installedVersion": "0.9.0", "runningVersion": "0.9.0",
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
