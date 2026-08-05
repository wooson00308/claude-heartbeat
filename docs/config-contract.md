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

## 버전 표면

설치본 버전은 두 명령이 같은 한 줄로 낸다. 출력은 `heartbeat <X.Y.Z>` 형식,
stdout, 종료 코드 0이다.

```
$ heartbeat --version
heartbeat 0.8.0
$ heartbeat version
heartbeat 0.8.0
```

두 형태를 다 두는 이유는 찾는 손이 갈리기 때문이다(플래그로 찾는 쪽과 서브커맨드로
찾는 쪽). 파싱하는 쪽은 마지막 공백 뒤를 버전으로 읽으면 된다.

버전의 단일 원천은 `src/heartbeat/__init__.py`의 `__version__`이다. 빌드
메타데이터는 hatchling이 여기서 읽어 가고(`[tool.hatch.version]`), 런타임도 같은
상수를 쓴다. `importlib.metadata.version("claude-heartbeat")`는 원천이 아니다 —
editable 설치의 메타데이터는 마지막 `pip install -e` 시점에 굳고 그 뒤 git pull로
코드가 올라가도 따라오지 않는다(2026-08-05 실측: 메타데이터 0.5.1 / 코드 0.8.0).
소비자가 알고 싶은 것은 "지금 도는 코드가 몇 버전인가"이므로 코드와 같이 움직이는
값이 맞는 답이다.

### state.json의 `_daemon`

`state.json` 최상위에서 밑줄로 시작하는 키는 데몬 예약 영역이고 잡 이름이 아니다.
잡 목록을 훑는 도구는 이 키들을 건너뛰어야 한다. 지금 있는 예약 키는 `_daemon`
하나이며, 예약 키가 느는 것은 하위호환 변경으로 취급한다.

`_daemon`은 데몬이 기동할 때마다 덮어쓰인다.

| 키 | 내용 |
|---|---|
| `version` | 그 프로세스가 물고 있는 `__version__` |
| `pid` | 데몬 프로세스 ID |
| `started_at` | 기동 시각 (ISO 8601, 로컬 시간) |

데몬이 한 번도 뜬 적 없으면 키 자체가 없다. 종료 시 지워지지 않으므로 `_daemon`의
존재는 "지금 돌고 있다"가 아니라 "마지막으로 뜬 데몬이 이랬다"는 뜻이다. 살아
있는지는 `heartbeat status`로 확인한다.

읽는 쪽이 쓰는 용도는 하나다. `_daemon.version`이 `heartbeat --version`과 다르면
디스크의 코드는 갱신됐는데 메모리의 프로세스는 옛 코드라는 뜻이다(2026-08-05 실측
사고의 모양). 그 상태를 푸는 것이 아래 `heartbeat update`다.

## `heartbeat update` 계약

editable(git) 설치본을 갱신하고, 필요하면 데몬을 새 코드로 재기동한다. 사람 대신
앱이 부르는 명령이라 출력 자체가 계약이다.

### 출력 규격

- stdout에는 계약 줄만 나간다. 사람이 읽을 진단·가이드는 전부 stderr로 간다.
  앱은 stdout만 파싱하고, stderr는 그대로 사용자에게 보여주면 된다.
- 계약 줄은 공백으로 나뉜 `key=value` 목록이다. 값에는 공백이 없다(값 안의 공백은
  `_`로 치환된다). 키는 낸 순서 그대로다.
- 줄 구성은 단계 줄 0~3개 다음에 `result=` 줄 정확히 하나다. `result=` 줄은 항상
  마지막이다 — 앱은 마지막 줄만으로 판정할 수 있고, 단계 줄은 진행 표시용이다.
- 단계 줄은 `step=repo` → `step=deps` → `step=service` 순서로만 나온다. 앞 단계가
  실패하면 뒤 단계 줄은 나오지 않는다.
- 모르는 key는 무시한다. 키 추가는 하위호환 변경이다 — 예를 들어 `detail=updated`
  줄에는 `from=`·`to=`가 더 붙고, service 줄에는 `label=`이 붙을 수 있다.

### `step=repo` — 저장소 갱신

`git fetch` 후 upstream으로 fast-forward만 한다. merge도 rebase도 하지 않는다.

| status | detail | 뜻 |
|---|---|---|
| `ok` | `up-to-date` | HEAD가 이미 upstream과 같다. 갱신 없음 |
| `ok` | `updated` | fast-forward 완료. `from=`·`to=`에 짧은 커밋 해시가 붙는다 |
| `failed` | `not-a-git-repo` | git 설치본이 아니거나(wheel 설치) git 명령이 없다 |
| `failed` | `dirty-tree` | 추적 중인 파일에 미커밋 변경이 있다 |
| `failed` | `no-upstream` | 현재 브랜치에 upstream이 없어 당길 대상을 모른다 |
| `failed` | `fetch-failed` | fetch 실패 또는 git 명령 타임아웃(120초) |
| `failed` | `non-fast-forward` | 로컬 커밋이 갈라져 ff가 불가능하다 |
| `failed` | `merge-failed` | ff 판정은 통과했는데 병합이 실패했다 |

wheel 설치본은 이 명령의 대상이 아니다 — `detail=not-a-git-repo`로 끝나고,
갱신은 pip 몫이다.

### `step=deps` — 의존성 반영

HEAD가 움직였을 때만 `pip install -e <root>`를 돈다. 새 의존성뿐 아니라 `pip
install -e` 시점에 굳는 패키지 메타데이터도 여기서 같이 갱신된다.

| status | detail | 뜻 |
|---|---|---|
| `ok` | `reinstalled` | editable 재설치 완료 |
| `skipped` | `not-needed` | HEAD가 안 움직여 돌 이유가 없다 |
| `failed` | `pip-timeout` | pip이 600초 안에 안 끝났다 |
| `failed` | `pip-failed` | pip이 non-zero로 끝났다 |

### `step=service` — 데몬 재기동

재기동은 두 경우에 필요하다고 본다. (1) HEAD가 움직였다. (2) HEAD는 그대로인데
도는 데몬의 `_daemon.version`이 디스크 버전과 다르다. 후자를 보는 이유는 그것이
2026-08-05 사고의 모양이기 때문이다 — pull이 공회전이어도 프로세스는 확인한다.

대상은 코드가 아는 표준 이름이 아니라 이 머신에 실제로 등록된 서비스다(launchd는
`~/Library/LaunchAgents`의 heartbeat 계열 plist, systemd는 user unit, Windows는
등록된 task). 이름을 알아냈으면 `label=<이름>`이 줄에 붙는다.

| status | detail | 뜻 |
|---|---|---|
| `ok` | `restarted` | 재기동 성공 |
| `skipped` | `not-needed` | 코드도 그대로, 도는 데몬 버전도 일치 |
| `skipped` | `not-registered` | OS 스케줄러에 등록된 서비스가 없다 |
| `skipped` | `not-loaded` | 등록은 됐는데 로드/기동 상태가 아니다. 다음 기동 때 새 코드로 뜬다 |
| `skipped` | `self-restart-blocked` | 데몬 자신의 프로세스 트리 안에서 불렸다 |
| `failed` | `restart-failed` | 재기동 명령이 non-zero로 끝났다 |
| `failed` | `launchctl-missing` / `systemctl-missing` / `schtasks-missing` | OS 스케줄러 명령을 찾을 수 없다 |
| `failed` | `unsupported-platform` | 이 OS용 어댑터가 없다 |

`self-restart-blocked`는 데몬이 돌린 잡 안에서 update를 부른 경우다. 재기동이 그
잡 프로세스 트리째 죽이고, 죽는 시점이 계약 줄을 다 내기 전이면 앱은 결과도 진행도
알 수 없다. 그래서 그 경로는 재기동하지 않고 명시적으로 실패시킨다 — 데몬 밖(앱·
터미널)에서 다시 부르면 된다.

Windows(Task Scheduler)는 `not-loaded`를 구분하지 않는다. schtasks의 실행 상태
문자열이 로케일 의존이라 파싱을 계약에 넣을 수 없다.

### `result=` 줄과 종료 코드

마지막 줄은 항상 `result=<값> version=<X.Y.Z> exit=<코드>` 세 키다.

- `version`은 갱신이 끝난 뒤 **디스크에 있는** 버전이다. 지금 프로세스에 import된
  값이 아니다 — pull 이전 코드의 값을 보고하면 "갱신했다면서 옛 버전"이 된다.
  git 설치본이 아닌 경우에만 도는 프로세스의 버전이 실린다.
- `exit`은 프로세스 종료 코드와 같은 값이다. 파이프가 끊긴 상황을 대비해 줄에도 싣는다.

| result | 뜻 |
|---|---|
| `ok` | 갱신·재기동이 필요한 만큼 다 끝났다 |
| `partial` | 코드는 갱신됐는데 뒤가 못 따라왔다. 사람 손이 필요하다 |
| `failed` | 저장소 단계에서 멈췄다. 바뀐 것이 없다 |

| exit | result | 원인 |
|---|---|---|
| 0 | `ok` | 성공 |
| 10 | `failed` | git 저장소가 아님 / git 명령 없음 |
| 11 | `failed` | 미커밋 변경 |
| 12 | `failed` | fast-forward 불가 (`non-fast-forward`, `merge-failed`) |
| 13 | `failed` | fetch 실패·타임아웃 |
| 14 | `failed` | upstream 없음 |
| 20 | `partial` | 의존성 설치 실패 |
| 30 | `partial` | 서비스 재기동 실패 |
| 31 | `partial` | 데몬이 OS 스케줄러 밖에서 돌아 재기동 못 함 |
| 32 | `partial` | 데몬 자신의 트리 안에서 불려 재기동 안 함 |

10번대는 저장소, 20번대는 의존성, 30번대는 프로세스다. 새 원인이 생기면 같은
자리수 안에서 번호가 는다.

### 예시

정상 갱신:

```
step=repo status=ok detail=updated from=25e372e to=a91f0c4
step=deps status=ok detail=reinstalled
step=service status=ok detail=restarted label=com.claude-heartbeat
result=ok version=0.8.1 exit=0
```

이미 최신이고 도는 데몬도 같은 버전:

```
step=repo status=ok detail=up-to-date
step=deps status=skipped detail=not-needed
step=service status=skipped detail=not-needed
result=ok version=0.8.0 exit=0
```

미커밋 변경이 있어 멈춤 (진단은 stderr):

```
step=repo status=failed detail=dirty-tree
result=failed version=0.8.0 exit=11
```

## 마이그레이션

`heartbeat migrate`가 `HEARTBEAT.md`의 잡을 slug별 `jobs.d/<slug>.md`로
분리한다. slug 없는 블록·전역 설정·HTML 주석(외부 도구의 마커)은 원본에
남고, 원본은 실행 전 백업된다. `--dry-run`으로 미리 볼 수 있다.
