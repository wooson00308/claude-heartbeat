# 런타임 관리 계약

앱은 기기의 설치 상태와 업데이트를 이 계약으로만 다룬다. SQLite, plist, systemd unit, Task Scheduler
출력을 직접 읽거나 파싱하지 않는다. 프로젝트별 설정과 실행 계약은 `agent-runtime-contract.md`에 있고,
이 문서는 기기 단위 조회와 업데이트만 다룬다.

## 명령 목록의 정의 자리

계약이 알리는 명령 목록은 `agent_contract.py` 한 곳에서만 정해진다. 응답을 만드는 층은 그 목록을 읽기만
하고 덧붙이거나 지우지 않으며, 같은 목록이 라우팅 기준이기도 하다. 목록에 있는 이름은 처리되고 목록에
없는 이름은 `unsupported_command` 실패 봉투를 받는다.

- `implementedCommands`: 표준 입력 JSON으로 호출하는 agent 명령. 여기에 `update.plan`과 `update.apply`가
  포함된다.
- `runtimeCommands`: 독립 실행형 런타임의 `heartbeat runtime` 명령군. 기기 조회는 이쪽이 책임진다.
- `reservedCommands`: 이름만 잡아 둔 명령. 현재는 비어 있다.

## 기기 조회

기기 조회는 `heartbeat runtime inspect`가 제공하며 응답 필드와 뜻은 `agent-runtime-contract.md`의
`기기 상태 조회` 절이 정본이다. 같은 사실을 돌려주는 두 번째 명령은 만들지 않는다. 업데이트 계획도 같은
사실을 다시 정의하지 않고 그 조회가 쓰는 값을 그대로 인용한다.

## 업데이트 계획

```json
{"apiVersion":"1","requestId":"request-300","installRoot":"/…/install","versionDir":"/…/install/versions/0.9.0"}
```

`update.plan`은 아무것도 바꾸지 않는다. 응답 `data`는 다음을 담는다.

- `planId`: 계획이 가정한 사실의 지문이다. 대상 manifest, 현재 설치 버전과 설치 판정, 실행 중 버전,
  서비스 신원(결과·식별자·실행 경로·등록·실행 여부), 영향 실행 수와 프로젝트 목록을 묶어 해시한다.
  조회 시각은 지문에 넣지 않는다. 읽을 때마다 달라져 모든 계획이 즉시 낡아 버리기 때문이다.
- `result`: `ready`, `verification_failed`, `unsupported_version`, `candidate_missing` 중 하나.
- `targetVersion`, `target`, `manifestVerified`, `installedVersion`, `runningVersion`.
- `launcherSwitchRequired`, `serviceTransitionRequired`, `recoverableOnFailure`.
- `activeRuns`와 `projects`: 영향받는 실행 수와 중복 없는 프로젝트 식별자 목록이다. prompt, 이벤트 원문,
  도구 출력, 인증 정보는 담지 않는다. 저장소가 이 경로로 그 값을 내보내지 않는다.
- `service`: 기기 조회와 같은 서비스 상태 구조. 플랫폼 고유 이름과 명령 출력은 `detail`에만 있다.
- `stages`: 적용이 지날 단계 이름의 순서. 앱은 이 이름을 그대로 쓰고 합치거나 새로 만들지 않는다.

## 업데이트 적용

```json
{"apiVersion":"1","requestId":"request-301","installRoot":"…","versionDir":"…","planId":"…","confirmed":true}
```

`update.apply`는 직전 계획 식별자와 명시적 확인을 요구한다. 실행 중 작업이 없어도 계획과 적용은 분리된다.
적용 직전에 같은 사실을 다시 읽어 지문을 비교하고, 다르면 아무것도 쓰지 않고 `plan_stale`로 끝난다.

단계는 항상 다음 순서이며 세 운영체제가 같은 이름과 같은 뜻을 쓴다.

1. `manifest_verification`
2. `version_install`
3. `launcher_switch`
4. `service_transition`
5. `running_version_check`

각 단계의 `status`는 `ok`, `failed`, `skipped` 중 하나다. 등록된 서비스가 없으면 4단계는 `skipped`이며
실패가 아니다.

`result`는 다음과 같다.

- `success`: 모든 단계가 `ok`이거나 `skipped`다.
- `partial_success`: launcher는 새 버전을 가리키는데 뒤 단계가 끝나지 않았다. `runnableVersion`에 지금
  실행 가능한 버전이, `recoveryActions`에 사용자가 수행할 행동이 온다. 전체 성공으로 표시하지 않는다.
- `failure`: launcher가 움직이기 전에 멈췄다. 기존 launcher와 서비스가 그대로이고 `runnableVersion`은
  이전 설치 버전이다.
- `plan_stale`: 지문이 달라져 0단계에서 중단했다. 새 계획이 필요하다.
- `confirmation_required`: 확인이 없어 아무것도 하지 않았다.

적용은 실행 중 작업을 자동으로 종료하지 않는다. 영향이 남아 있어도 확인 범위를 넘어선 종료를 만들지
않는다.

## 호환성

- manifest와 stable launcher 형식은 기존 것을 그대로 쓴다. 앱 전용 사본을 만들지 않는다.
- 응답의 모르는 선택 필드는 무시한다. 같은 API 주 버전 안에서는 필드가 늘어날 수 있다.
- Dream, editable checkout 업데이트, 일반 jobs.d 실행은 이 계약의 상태 판정과 적용 대상이 아니다.
