# claude-heartbeat

_세션 사이에서도 Claude를 살려두세요._

**[English](../README.md)**

---

Claude Code는 반응형입니다. 대화할 때만 작동합니다.
Heartbeat는 이를 능동형으로 바꿔줍니다.

주기적으로 Claude를 깨워 스킬을 실행하고 다시 잠드는 경량 데몬입니다. 할 일이 없으면 토큰 비용이 발생하지 않습니다.

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  HEARTBEAT  │     │   condition  │     │  claude -p  │
│  .md        │────►│   check      │────►│  "{prompt}" │
│ (job config)│     │  (shell cmd) │     │  (skill run)│
└─────────────┘     └──────────────┘     └─────────────┘
                      할 일 없으면          필요할 때만
                      스킵 (비용 0)         깨움
```

---

## 동작 방식

1. Heartbeat 데몬이 OS 백그라운드 스케줄러로 실행됩니다 (macOS launchd / Windows Task Scheduler / Linux systemd)
2. 60초마다 등록된 잡을 확인합니다
3. interval이 경과한 잡에 대해 condition을 체크합니다
4. condition을 통과하면 `claude -p "{prompt}"`로 Claude를 깨웁니다
5. Claude가 스킬을 실행하고 다시 잠듭니다

데몬 자체는 LLM을 호출하지 않습니다. 언제 깨울지만 판단합니다.

## 무엇을 실행할 수 있나요?

`prompt` 필드에는 한 줄 명령, 스킬 호출, 문서 참조 등 단일 라인 값을 넣을 수 있습니다. Heartbeat는 그 내용을 그대로 `claude -p`에 전달합니다. 긴 프롬프트나 멀티스텝 작업은 Claude 스킬로 작성한 뒤 `prompt: /your-skill`처럼 참조하는 게 정석입니다.

### 평문 프롬프트

```markdown
## daily-summary
- slug: -Users-yourname-Git-myproject
- prompt: 지난 24시간 git log 확인하고 변경사항 요약해줘
- interval: 1d
- timeout: 5m

## lint-check
- slug: -Users-yourname-Git-myproject
- prompt: npm run lint 돌려보고 에러 있으면 정리해줘
- interval: 6h
- timeout: 3m
```

### 스킬

Claude Code는 [사용자 정의 스킬](https://docs.anthropic.com/en/docs/claude-code)을 지원합니다. 재사용 가능한 프롬프트를 만들어 Claude가 필요할 때 실행할 수 있는 프로토콜입니다. 더 복잡하거나 여러 단계가 필요한 작업은 스킬로 작성하여 prompt 필드에서 참조할 수 있습니다.

기본 제공되는 스킬은 `dream`과 `heartbeat-register` 두 가지입니다.

```bash
heartbeat skills              # 사용 가능한 스킬 목록
heartbeat install dream       # 스킬 설치
```

### dream (예시 스킬)

세션 transcript를 자동으로 정제하여 장기 기억에 반영합니다. Claude Code는 매 대화를 JSONL로 저장하지만 다음 세션에서 다시 읽지 않습니다. dream 스킬이 이 transcript를 처리하여, 다음 세션이 시작될 때 이전 맥락을 이미 알고 있는 상태로 만들어줍니다.

자세한 내용은 [skills/dream/README_ko.md](../skills/dream/README_ko.md)를 참고하세요.

### heartbeat-register (헬퍼 스킬)

자연어 한 줄 요청을 HEARTBEAT.md 잡으로 등록해줍니다. 단순 명령은 prompt에 바로 박고, 멀티스텝 작업은 사용자 동의를 받은 뒤 새 SKILL.md를 만들어 잡과 페어로 등록합니다. "하루 5번"처럼 quota 표현이 들어가면 `max_per: 5/24h`를 자동으로 박아줍니다.

```bash
heartbeat install heartbeat-register
```

자세한 내용은 [skills/heartbeat-register/README.md](../skills/heartbeat-register/README.md)를 참고하세요.

---

## 요구사항

- macOS / Windows / Linux
- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)

## 빠른 시작

```bash
pip install claude-heartbeat

# dream 스킬 설치 (SKILL.md 복사 + heartbeat 잡 자동 등록)
heartbeat install dream

# 확인
heartbeat jobs

# 테스트 실행
heartbeat once

# OS 백그라운드 스케줄러에 등록 (launchd / Task Scheduler / systemd 자동 감지)
heartbeat install-service

# 또는 테스트용 포그라운드 실행
heartbeat start
```

수동 설정, 로그 경로, launchd / Task Scheduler 상세는 [설정 가이드](setup.md)를 참고하세요.

## 설정

`~/.claude/HEARTBEAT.md`에 잡을 등록합니다:

```markdown
# HEARTBEAT

- tick: 60s

## daily-summary
- slug: -Users-yourname-Git-myproject
- prompt: 지난 24시간 git log 확인하고 변경사항 요약해줘
- interval: 1d
- timeout: 5m
- notify: failure

## llm-news-digest
- slug: -Users-yourname-Git-myproject
- prompt: /llm-news-digest
- interval: 1h
- timeout: 10m
- max_per: 5/24h
- notify: failure
```

| 필드      | 설명                                                                | 기본값            |
|-----------|---------------------------------------------------------------------|-------------------|
| slug      | 프로젝트 슬러그 (`~/.claude/projects/` 하위 디렉토리명)              | 필수              |
| prompt    | `claude -p`에 전달할 프롬프트                                       | 필수              |
| interval  | 실행 간격 (s/m/h/d)                                                 | 1h                |
| timeout   | 타임아웃 (s/m/h/d)                                                  | 600s              |
| condition | 실행 전 셸 체크 (exit 0이면 실행)                                   | 없음 (항상 실행)  |
| notify    | 데스크탑 알림 수준: `all`, `failure`, `none`                        | all               |
| max_per   | 슬라이딩 윈도우 quota (예: `5/24h` = 지난 24시간 안 최대 5회 실행) | 없음 (quota 없음) |

## CLI

```bash
heartbeat start                # 포그라운드 실행 (백그라운드는 OS 스케줄러에 위임)
heartbeat stop                 # 실행 중인 heartbeat 종료
heartbeat status               # 상태 + 잡별 이력 + 최근 로그
heartbeat jobs                 # 등록된 잡 목록
heartbeat once                 # 모든 잡 1회 실행
heartbeat once -j "name"       # 특정 잡 1회 실행
heartbeat skills               # 사용 가능한 스킬 목록
heartbeat install <name>       # 스킬 설치
heartbeat install-service      # OS 백그라운드 스케줄러에 등록 (launchd / Task Scheduler / systemd)
heartbeat uninstall-service    # OS 스케줄러에서 해제
```

## v0.1에서 마이그레이션

`dream-preprocessor` v0.1에서 업그레이드하는 경우:

- `dream-heartbeat`은 `heartbeat`의 별칭으로 계속 동작합니다
- `dream-prep`도 기존과 동일하게 동작합니다
- `HEARTBEAT.md`나 launchd plist를 수정할 필요가 없습니다

## 라이선스

MIT

---

_under the moonlight, Claude dreams._
