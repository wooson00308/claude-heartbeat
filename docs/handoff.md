# 인수인계

- 현재 범위: Agent API v1 기반 자동 배정 dispatcher, 읽기 전용 자격 대기열, watcher 안전망, 모델 카탈로그, DB schema v5, 서비스 이전·복구.
- 상태: 로컬 구현·전체 회귀·macOS universal2 패키징·실서비스 전환 완료. 원격 릴리스와 태그는 변경하지 않음.
- 런타임: v0.9.0, API 1, DB schema 5. 자동 배정은 프로젝트별 `automationEnabled` opt-in이며 workflow-labs 실제 값은 꺼짐이다.
- 서비스: `com.claude-heartbeat`가 앱 stable launcher의 `agent-dispatcher`를 실행한다. 강제 재시작 뒤 stale dispatcher 행을 안전하게 회수하고 새 PID로 소유권을 넘긴다. 앱 종료 뒤에도 서비스가 유지된다.
- 기존 설정: workflow-labs의 앱 관리 역할 잡만 이전되어 기존 jobs.d 파일이 제거됐다. mech-arena 잡과 외부 Dream plist는 보존됐고 외부 label은 disabled 상태다.
- 패키지: `dist/heartbeat`에 v0.9.0 macOS universal2 빌드가 있다. manifest와 heartbeat/Python/psutil/watchdog arm64+x86_64를 확인했다.
- 검증: 전체 330 passed, 8 skipped. 변경 범위 Ruff 통과. `launchctl kickstart -k`에서 PID와 DB dispatcher 신원이 함께 갱신되고 runningVersion 0.9.0을 확인했다.
- 남은 단계: Linux·Windows 실제 서비스 smoke와 공식 3OS release 산출물 양성 경로는 target CI에서 확인한다. 로컬 자동 배정은 사용자가 켜기 전까지 시작하지 않는다.
