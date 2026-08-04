# 설정 계약 (config contract)

외부 도구(앱·스크립트)가 하트비트 잡을 설치·수정할 때 기대도 되는 것들을 적는다.
이 문서에 적힌 것은 semver를 따른다 — 1.0 이후에는 주 버전이 오르기 전까지 깨지 않는다.
적히지 않은 내부 동작(로그 형식, 아래 절이 명시하지 않은 state.json 키 등)은 계약이 아니다.

## 파일 위치와 소유

| 파일 | 소유 | 내용 |
|---|---|---|
| `~/.claude/HEARTBEAT.md` | 공유(레거시) | 전역 설정(`- tick:`) + 하위호환 잡 정의 |
| `~/.claude/heartbeat/jobs.d/<slug>.md` | slug 소유자 | 그 프로젝트의 잡 정의 (v0.8.0+) |

외부 도구는 자기 프로젝트의 `jobs.d/<slug>.md` 파일 하나를 통째로 쓴다.
다른 파일을 읽고 병합할 필요가 없고, 해서도 안 된다. 한 파일을 여러 도구가
나눠 쓰는 구조(마커 블록, 부분 병합)는 지원하지 않는다 — 그 구조가 서로의
잡을 지우는 사고의 원인이었다.

`jobs.d` 디렉토리는 쓰는 쪽이 만든다(`mkdir -p`와 같은 생성). `heartbeat init`도
만들어 둔다. 데몬은 디렉토리가 없으면 없는 대로 동작한다. 디렉토리 안에서 읽는
것은 일반 `.md` 파일뿐이고, 하위 디렉토리와 다른 확장자는 무시된다.

## 잡 문법

```markdown
## <잡 이름>
- prompt: <claude -p로 넘길 한 줄>
- interval: <30m | 2h | 1d ...>
- timeout: <20m ...>
- condition: <shell 명령. exit 0이면 실행>
- notify: <all | failure | none>
- model: <claude --model 값. 생략 시 CLI 기본>
- max_per: <N/24h 슬라이딩 윈도우 한도. 생략 시 무제한>
```

- jobs.d 파일 안의 잡은 파일 이름의 slug 소속으로 강제된다. `- slug:` 줄은
  필요 없고, 적어도 파일 이름과 다르면 경고 후 파일 이름이 이긴다.
- `prompt`가 없는 잡은 무시된다.
- 잡 이름은 실행 이력(state.json)의 키다. 이름을 바꾸면 실행 이력과 quota
  윈도우가 초기화된다.

## 병합 우선순위

1. `jobs.d/*.md` (파일 이름 정렬 순서, 먼저 읽은 파일이 이김)
2. `HEARTBEAT.md`

잡 이름이 겹치면 위 순서대로 하나만 남고 경고 로그가 남는다.
전역 설정(`tick`)은 `HEARTBEAT.md`만 읽는다.

## condition의 실행 환경과 사유 통로

- condition은 slug가 가리키는 프로젝트 루트를 cwd로 `shell`에서 실행된다.
- exit 0 → 실행, 그 외 → 스킵. 타임아웃(10s)·실행 실패도 스킵(fail-closed).
- stdout 첫 줄(최대 200자)은 `state.json`의 잡 항목 `last_condition_output`에
  실린다. 스킵 사유를 사용자에게 보여주고 싶은 도구는 condition이 사유를
  출력하게 하면 된다. 빈 출력이면 키가 지워진다.

## 실행 모델

- 같은 slug의 잡은 순차 실행된다(파일 충돌 회피).
- 다른 slug는 서로 독립이다. 한 프로젝트의 장기 세션이 다른 프로젝트의
  스케줄을 늦추지 않는다(v0.8.0+).
- 같은 slug 그룹이 실행 중이면 그 그룹은 due여도 그 tick을 건너뛴다.
  데몬 안에서는 같은 잡이 겹쳐 실행되지 않는다. `heartbeat once`는 데몬과
  별개 프로세스라 이 보장 밖이다 — 데몬 실행 중에 돌리면 CLI가 경고한다.

## state.json에서 계약인 키

`~/.claude/heartbeat/state.json`의 잡별 항목 중 외부 도구가 읽어도 되는 키는
다음뿐이다. 이 밖의 키는 내부 구현이다.

- `last_run` — 마지막 판정·실행 시각 (ISO 8601)
- `last_result` — `success` | `failure` | `timeout` | `skipped` | `quota_skipped`
- `last_duration` — 초 단위 소요 시간
- `last_condition_output` — 마지막 condition의 stdout 첫 줄. 없으면 키 자체가 없다.
- `recent_runs` — `max_per` 판정에 쓰는 epoch 초 배열 (한도 있는 잡만)

파일 쓰기는 원자적 교체다. 읽는 쪽에서 도중 상태(잘린 JSON)를 만나지 않는다.

## 마이그레이션

`heartbeat migrate`가 `HEARTBEAT.md`의 잡을 slug별 `jobs.d/<slug>.md`로
분리한다. slug 없는 블록·전역 설정·HTML 주석(외부 도구의 마커)은 원본에
남고, 원본은 실행 전 백업된다. `--dry-run`으로 미리 볼 수 있다.
