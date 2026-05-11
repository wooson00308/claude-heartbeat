# dream 스킬

_잠을 자야 기억이 남는다._

**[English](README.md)**

---

```
  Transcript        dream-prep         /dream             Memory
  (raw JSONL)       (preprocess)       (consolidate)      (topic files)

  Session 1 ─┐
  Session 2 ─┤
  Session 3 ─┼─────► Markdown ─────► ┌─ Orient ─────────►  MEMORY.md
  Session 4 ─┤       (compact)       │  Gather             user.md
  Session 5 ─┘                       │  Consolidate        feedback.md
                                     └─ Prune              project.md

      L3                                L2                     L1
  (raw logs)                        (knowledge)              (index)
```

---

## 문제

Claude Code는 대화할 때마다 모든 내용을 transcript JSONL로 저장하지만, 이 파일을 다음 세션에서 다시 읽는 기능은 없습니다. 새 세션이 시작되면 Claude는 백지 상태가 되어, 어제 4시간 동안 같이 디버깅한 내용도, 지난주에 내린 아키텍처 결정도 기억하지 못합니다.

현재 Claude Code가 세션 간에 유지하는 기억은 두 가지뿐입니다:

| 저장소 | 자동 로드 | 한계 |
|--------|-----------|------|
| CLAUDE.md | 전체 | 사용자가 직접 작성하고 관리해야 합니다 |
| MEMORY.md | 처음 200줄만 | topic 파일은 수동으로 열어야 읽힙니다 |

모든 맥락은 이미 transcript에 들어있고, 메모리 시스템도 이미 존재합니다. 둘 사이를 연결하는 것만 빠져 있습니다.

## 해결

사람은 낮에 경험하고 밤에 잠을 자면서 기억을 정리합니다. 해마가 하루치 경험을 훑으며 쓸 만한 것은 장기기억으로 보내고, 나머지는 버립니다.

dream은 이와 같은 일을 Claude에게 시킵니다. 주기적으로 transcript를 읽어서 노이즈를 걷어내고, 다음 대화에서 바로 쓸 수 있는 기억으로 만들어줍니다.

"기억해"라고 말하지 않아도 기억하고, "정리해"라고 말하지 않아도 정리합니다.
그래서 이름이 dream입니다.

---

## 과정

### 1. Heartbeat --- 깨울지 판단합니다

```
[heartbeat daemon]
     │
     ├─ 60초마다 깨어남
     ├─ HEARTBEAT.md에서 잡 목록 파싱
     ├─ 잡별 interval 확인 (예: 3시간)
     └─ condition 체크
         └─ "미처리 transcript가 있는가?"
              ├─ 없으면 → 스킵 (비용 0)
              └─ 있으면 → 다음 단계
```

데몬 자체는 LLM을 호출하지 않기 때문에 상주해도 토큰 비용이 발생하지 않습니다. 할 일이 있을 때만 Claude를 깨웁니다.

### 2. dream-prep --- 전처리합니다

transcript JSONL은 그대로 LLM에 넣기엔 너무 크고 노이즈가 많습니다. dream-prep이 유저/어시스턴트 텍스트만 추출하고, 코드 블록을 압축하고, 연속 도구 호출을 합치고, 시스템 메시지를 제거하여 경량 마크다운으로 변환합니다.

수천 줄짜리 JSONL이 LLM이 바로 읽을 수 있는 형태로 바뀝니다.

### 3. /dream --- 정제합니다

Claude가 깨어나서 `/dream` 스킬을 실행합니다. KAIROS autoDream의 4단계를 따릅니다:

| 단계 | 이름 | 하는 일 |
|------|------|---------|
| 1 | Orient | 현재 메모리 상태를 파악합니다 |
| 2 | Gather | 전처리된 마크다운을 읽습니다 (마킹은 dream-prep CLI가 보장) |
| 3 | Consolidate | 기존 메모리와 병합하여 topic 파일을 생성하거나 수정합니다 |
| 4 | Prune & Index | 중복을 제거하고 MEMORY.md 인덱스를 갱신합니다 (LLM은 타임스탬프만 갱신) |
| 5 | Lint | 5종 check: 고아/stale/모순/중복/frontmatter |

### 4. Memory --- 결과

정제가 끝나면 메모리가 갱신된 상태이므로, 다음 세션에서 Claude는 이전 대화의 맥락을 이미 알고 있습니다.

```
Before dream:
  "이 프로젝트 구조가 어떻게 되어있어?"  ← 매 세션마다 반복

After dream:
  (이미 알고 있음. 바로 작업 시작.)
```

---

## 메모리 계층

| 계층 | 설명 |
|------|------|
| L1: MEMORY.md | 항상 로드되는 200줄 인덱스입니다. |
| L2: Topic Files | 정제된 지식이며, 필요할 때 참조됩니다. (user.md, feedback.md, project.md ...) |
| L3: Transcript JSONL | 대화 원본입니다. 자동 저장되고, 자동 정제되며, 수동 개입이 필요 없습니다. |

---

## 설치

```bash
heartbeat install dream
```

실행하면:
1. SKILL.md를 `~/.claude/skills/dream/`에 복사합니다
2. 감지된 프로젝트마다 `~/.claude/HEARTBEAT.md`에 dream 잡을 등록합니다
3. `dream-prep` CLI를 활성화합니다

## 수동 사용

```bash
dream-prep list                            # 프로젝트별 transcript 수
dream-prep status --slug="-Users-yourname" # 처리 현황
dream-prep prep --slug="-Users-yourname" -n 5  # 전처리 실행
```

## 설정

heartbeat 잡은 기본적으로 3시간마다 실행됩니다. 미처리 transcript가 없으면 Claude를 깨우지 않으므로 비용이 발생하지 않습니다.

`~/.claude/HEARTBEAT.md`에서 interval, timeout, 알림 설정을 조정할 수 있습니다.
