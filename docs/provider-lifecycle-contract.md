# provider 실행 수명 계약

dispatcher는 provider 프로세스를 직접 다루지 않고 이 계약만 사용한다. 계약은 실행을 시작하고,
이벤트를 이어 읽고, 종료를 기다리고, 취소하고, 런타임 재시작 뒤 같은 프로세스인지 판정하는
다섯 단계로 나뉜다. 구현은 `src/heartbeat/providers/process.py`와
`src/heartbeat/providers/lifecycle.py`에 있고, 자동 검사는
`tests/test_agent_provider_lifecycle.py`에 있다.

계약 버전은 실행 핸들의 `PROVIDER_RUN_CONTRACT_VERSION`과 수명 모듈의
`PROVIDER_LIFECYCLE_CONTRACT_VERSION`이며 현재 값은 둘 다 1이다.

## 범위

이 계약은 실행 하나의 수명만 정의한다. 후보 선택, 프로젝트별 큐와 공정성, SQLite 영속화,
lease 갱신과 반납은 TASK-S051-04의 dispatcher에 남는다. 수명 모듈은 예약 응답을 읽고 실행이
무엇을 더 필요로 하는지 보고할 뿐, 예약하거나 반납하거나 저장하지 않는다.

## 실행 시작

`start_reserved_run(provider, reservation, request)`는 요청이 예약된 작업과 정확히 같을 때만
provider를 호출한다. 역할, 대상, lease 식별자 중 하나라도 다르면 provider를 건드리기 전에
`LifecycleFailure(stage="reservation", reason="reservation_mismatch")`를 돌려주므로 시작 호출은
0회다. 시작에 성공하면 종료를 기다리지 않고 `ProviderRunHandle`을 반환한다.

시작 성공은 프로세스가 생성됐고 prompt 전달 채널이 닫혔다는 뜻까지만이다. 역할 작업의 성공은
`conclude_run`이 돌려주는 `status`로만 판단한다.

실패는 단계로 구분한다.

| 반환 | stage | reason | 의미 |
| --- | --- | --- | --- |
| `LifecycleFailure` | `reservation` | `reservation_unavailable` | 예약 실패, 경합 소진, 마이그레이션 잠금 |
| `LifecycleFailure` | `reservation` | `reservation_usage_error` | 예약 헬퍼 호출 인자가 잘못됨 |
| `LifecycleFailure` | `reservation` | `reservation_malformed` | 예약 응답을 읽을 수 없음 |
| `LifecycleFailure` | `reservation` | `unsupported_reservation_contract` | 지원하지 않는 예약 계약 버전 |
| `LifecycleFailure` | `reservation` | `reservation_mismatch` | 요청이 예약된 작업과 다름 |
| `LifecycleFailure` | `provider_start` | `provider_<진단 상태>` | 진단 실패. 상태가 사유에 그대로 남는다 — `provider_executable_missing`, `provider_login_required`, `provider_unsupported_version`, `provider_billing_route_acknowledgement_required` 등 |
| `LifecycleFailure` | `provider_start` | `spawn` | 실행 파일을 시작하지 못함 |
| `LifecycleFailure` | `provider_start` | `prompt_delivery` | 프로세스는 생겼으나 prompt 전달 실패. 프로세스 트리는 정리한 뒤 반환한다 |
| `ProviderRunHandle` | - | - | 시작 성공 |

진단 실패에는 `start_failure.diagnostic`이 실려 있어 원인 상태를 그대로 읽을 수 있다. 진단이
상태를 싣지 못한 경우에만 사유가 단계 이름 `diagnostic`으로 남는다. 소비자 화면은 사유 코드를
사람 문장으로 옮기고, 모르는 코드는 원문 대신 일반 안내로 보여 준다.

## 실행 핸들

핸들은 dispatcher가 영속화할 값이며 다음을 가진다.

- `run_id`: 런타임 실행 식별자
- `provider`, `project_id`, `role`
- `target_id`, `lease_id`: 예약이 확인된 대상과 lease 식별자
- `pid`, `started_at`
- `process_identity`: 프로세스 생성 시각 기반 값. PID 재사용을 구분하는 근거다
- `event_path`: 이 실행 전용 append-only JSONL 파일
- `contract_version`

핸들에는 prompt 원문, 인증 토큰, API 키, 전체 환경을 넣지 않는다.

## 이벤트와 offset

실행마다 이벤트 파일 하나를 `run_id` 이름으로 만든다. 위치는 요청의 `event_root`이고, 지정이
없으면 런타임 소유 기본 경로를 쓰므로 다른 실행이나 프로젝트와 충돌하지 않는다. 줄바꿈을
`\n`으로 고정해 byte offset이 플랫폼 사이에서 같은 값을 가리킨다.

`watch(handle, offset=...)`는 offset 이후의 완결된 줄만 돌려주고 마지막 부분 줄은 완성될 때까지
보류한다. 같은 offset 재요청은 항상 같은 결과와 같은 `next_offset`을 낸다.

## 종료와 취소

`conclude_run(provider, handle, offset=...)`은 실행이 끝날 때까지 기다린 뒤 `RunConclusion`을
돌려준다. `cancel_run`은 같은 결과 형식을 쓰되 먼저 프로세스 트리를 정지시킨다. 두 결과 모두
`run_id`, `target_id`, `lease_id`를 실어 예약한 작업과 같은지 대조할 수 있게 한다.

정리 단계는 `remaining`에 남은 것만 순서대로 담는다.

- `process_termination`: 루트 프로세스가 사라졌음을 확인하지 못했다. 자식까지 확인되지 않은
  경우를 포함한다
- `event_close`: 이벤트 파일의 마지막 완결 이벤트가 종료 종류가 아니다
- `lease_release`: 이 실행에 lease가 있다. 반납은 호출자 몫이므로 lease가 있는 실행에서는 항상
  남는다

정상 취소는 `remaining`이 `("lease_release",)`이고, 종료를 확인하지 못하면 그 앞에
`process_termination`이 함께 남는다. 부분 성공을 전체 성공으로 표시하지 않는다.

이 계약은 lease 파일을 읽지도 쓰지도 않는다. `lease_release`는 남은 행동을 알리는 표시일 뿐이다.

## 복구

`recover_run(provider, handle, offset=...)`은 아무것도 시작하지 않고 저장된 핸들이 지금 무엇을
가리키는지 판정한다.

| outcome | reason | 판정 |
| --- | --- | --- |
| `resumed` | 없음 | PID가 살아 있고 생성 신원이 핸들과 같다. 새 provider를 시작하지 않고 offset부터 이어 읽는다 |
| `recovery_required` | `process_identity_mismatch` | PID는 같지만 생성 신원이 다르다. 다른 프로세스가 PID를 재사용했다 |
| `recovery_required` | `process_identity_unavailable` | 프로세스를 조회할 권한이 없어 신원을 확인하지 못했다 |
| `recovery_required` | `handle_identity_missing` | 핸들에 생성 신원이 없어 대조할 근거가 없다 |
| `cleanup_required` | `process_exited` | 프로세스가 끝났다. `remaining`에 남은 정리 단계를 담는다 |

확인하지 못한 것을 실행 중으로 추측하지 않는다. `observed_identity`에 실제로 관측한 값을 실어
판정 근거를 남긴다.

## 예약 응답

예약 성공 응답은 워크플로 예약 헬퍼가 소유하며 이 계약은 필드 이름과 의미를 바꾸지 않는다.
`read_reservation(exit_code, payload)`가 그 한 줄을 그대로 읽는다.

```json
{"contractVersion":1,"role":"developer","targetId":"TASK-1","leaseId":"lease-1-20260808000000","resultPrefix":"RES-20260808T000000Z-1-20260808000000","expiresAt":"2026-08-08T01:00:00Z","promptVersion":1,"rolePrompt":"You are the developer role for one pre-reserved LLM Workflow target. ..."}
```

`resultPrefix`는 역할이 새로 만들 문서 식별자의 접두어이고, `rolePrompt`는 역할 인계문이다.
인계문은 자식 프로세스의 표준 입력으로만 전달하며 핸들, 이벤트, 종료 결과, 일반 로그에 넣지
않는다.

예약 헬퍼는 예약 실패, 경합 소진, 마이그레이션 잠금을 모두 출력 없는 종료 코드 1로 답한다. 셋의
후속 행동이 같기 때문이며, 어느 경우에도 provider 시작 호출은 0회다.

## 민감정보

- 핸들, 이벤트 파일, 종료 결과, 일반 로그에 prompt 원문과 인증 값이 남지 않는다.
- provider 이벤트 세부 정보는 기존 민감정보 제거를 거치고 계약 밖 원문은 일반 이벤트로 보존하지
  않는다.
- 이벤트 파일 경로는 요청으로 지정할 수 있어 검사와 격리 실행이 사용자 홈을 쓰지 않는다.

## 호환성과 한계

- 기존 동기식 `run`은 시작·감시·대기 조합을 감싸는 호환 경로로 그대로 남는다. 기존 호출자의 결과
  상태와 Claude·Codex 이벤트 의미는 달라지지 않는다.
- 이벤트 파일은 런타임의 감시 스레드가 기록한다. 런타임이 죽으면 그 시점까지 기록된 이벤트는
  남지만 새 이벤트는 더 쌓이지 않는다. `resumed`는 같은 프로세스임을 확인하고 남은 이벤트를
  중복 없이 이어 읽는다는 뜻이며, 죽은 감시를 되살린다는 뜻이 아니다.
- 복구한 실행은 이 provider 객체가 시작한 실행이 아니므로 `cancel_run`의 결과 `detail`이
  `unknown_run`이 된다. 남은 정리 단계는 PID 관측과 이벤트 파일에서 그대로 판정하지만, 그
  프로세스를 PID로 정지시키는 일은 dispatcher가 정한다.
- 검증은 macOS에서 실행했다. Windows와 Linux 실행은 아직 실측하지 않았다.
