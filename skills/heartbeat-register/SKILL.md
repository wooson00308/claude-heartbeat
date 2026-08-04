---
description: 사용자 자연어 요청을 하트비트 잡(jobs.d/<슬러그>.md)으로 등록한다. 단순 명령이면 prompt 직접 박고, 복잡한 multi-step이면 사용자 동의 받고 새 SKILL.md 페어로 정착시킨다. "하트비트에 등록", "이거 매일 돌게", "/heartbeat-register" 등으로 트리거.
---

# /heartbeat-register — 자연어 잡 등록

## 핵심 원칙

1. 사용자 명시적 동의 없이 `~/.claude/skills/`에 SKILL.md를 만들지 않는다. 사용자 입장에서 모르는 스킬이 갑자기 늘어나는 건 곤란하다.
2. 사용자 명시적 동의 없이 `~/.claude/heartbeat/jobs.d/`의 잡 파일을 수정하지 않는다.
   (잡은 프로젝트별 파일 `jobs.d/{슬러그}.md`에 산다. `~/.claude/HEARTBEAT.md`는 레거시라
   새 잡을 거기 쓰지 않는다 — v0.8.0 계약, `docs/config-contract.md`.)
3. 모호한 부분은 1~2개 짧게 묻는다. 사람을 길게 잡지 않는다.

## 절차

### Phase 1: Classify

사용자 요청을 두 분기 중 하나로 분류한다.

- 단순(direct prompt): 외부 도구 단일 호출, 한 줄로 명확. 예: "git log 요약", "npm test 돌리고 알려줘", "dream-prep status 체크".
- 복잡(skill-worthy): 여러 스텝 / 외부 API / 출력 가공 / 일관된 포맷 필요. 예: "웹 리서치 → 취합 → 디스코드 포스트", "여러 PR 모니터링 → 카테고리 분류 → 슬랙 알림".

판단 애매하면 사용자에게 한 줄 묻는다: "이건 한 번 한 줄로 박아두면 될 것 같은데, 매번 일관된 결과가 필요하면 스킬로 빼는 게 좋아. 어떻게?"

### Phase 2A: 단순 분기 — prompt 직접 박기

1. 사용자에게 박을 잡 블록을 보여주고 동의 확인:

```markdown
## job-name
- prompt: {한 줄 명령}
- interval: {추출한 주기}
- timeout: 5m
- notify: failure
```

2. 동의(yes/ㅇㅇ/ㄱㄱ 등) 받으면 `~/.claude/heartbeat/jobs.d/{슬러그}.md` 끝에 추가
   (파일 없으면 생성. 파일 이름이 곧 slug라 잡 블록에 `- slug:` 줄은 쓰지 않는다).
3. `heartbeat jobs`로 등록 확인 출력.

### Phase 2B: 복잡 분기 — 스킬 페어 생성

1. 먼저 분류 결과 + 의도 사용자에게 한 줄로 보고:
   "이건 multi-step이라 한 줄 prompt로 박으면 매번 결과가 들쭉날쭉할 거야. `~/.claude/skills/{제안-스킬-이름}/SKILL.md`로 정착시키는 게 좋아 — 만들어도 될까?"
2. 사용자 동의 안 받으면 절대 SKILL.md 만들지 않는다. no면 단순 분기로 fallback (prompt 직접 + 한계 명시 한 줄).
3. yes면 모호한 부분 1~2개만 짧게 확인:
   - 어떤 외부 채널/타겟? (디스코드 채널 ID, 슬랙 채널, 파일 경로 등)
   - 입력 소스 우선순위? (있을 때만)
   - 출력 포맷 선호? (있을 때만)
4. `~/.claude/skills/{스킬-이름}/SKILL.md` 작성. frontmatter + 본문 절차:

```markdown
---
description: {한 줄 설명. 트리거 키워드 포함}
---

# /{스킬-이름}

## 절차

1. {단계 1 — 어떤 도구로 무엇}
2. {단계 2}
3. ...

## 주의
- {일관성 보장 포인트}
- {출력 포맷 약속}
```

5. `~/.claude/heartbeat/jobs.d/{슬러그}.md`에 잡 등록:

```markdown
## {스킬-이름}
- prompt: /{스킬-이름}
- interval: {추출한 주기}
- timeout: 10m
- notify: failure
```

6. quota 표현이 사용자 요청에 있었으면 `max_per: N/Mh` 추가 (Phase 3 참고).
7. 등록 후 `heartbeat jobs`로 확인.

### Phase 3: quota 인식

사용자 요청에 다음 표현이 있으면 `max_per` 필드를 박는다 (슬라이딩 윈도우).

| 사용자 표현 | max_per |
|-------------|---------|
| "하루 N번" / "1일 N회" | `N/24h` |
| "주 M번" / "1주 M회" | `M/7d` |
| "시간당 K번" | `K/1h` |
| 명시 없음 | (필드 생략) |

예: "1시간마다 하루 5번" → `interval: 1h` + `max_per: 5/24h`.

quota 의미 한 줄 안내: "지난 24시간 안에 5번까지만 깨움. 5번 다 채우면 그 다음 19시간 동안은 자동 skip."

### Phase 4: slug 자동 추출

현재 CWD에서 슬러그 도출 — 이 값이 잡 파일 이름(`jobs.d/{슬러그}.md`)이 된다:

1. Bash `pwd`로 절대 경로 받기.
2. `/`를 `-`로 교체, 맨 앞에 `-` 붙이기.
   예: `/Users/catze/Git/myproject` → `-Users-catze-Git-myproject`
3. 사용자가 다른 프로젝트 슬러그 명시했으면 그걸 우선.

### Phase 5: 등록 후 확인

1. `heartbeat jobs` 출력 사용자에게 보여주기.
2. condition 박은 게 있으면 `heartbeat once -j {잡-이름}` 한 번 돌려보길 권장 (실제 실행되는지 확인). 다만 자동 실행은 안 함 — 사용자 결정.

## 흐름 예시

사용자: "일일퀘스트로 하루 5번 1시간 간격으로 LLM 소식 웹 리서치하고 디스코드 #ai-news에 포스트해줘"

응답:
1. (Phase 1) 분류: 복잡(웹 리서치 + 취합 + 디스코드 포스트).
2. (Phase 2B-1) "이건 multi-step이라 `~/.claude/skills/llm-news-digest/SKILL.md`로 정착시키는 게 좋아. 만들어도 될까?"
3. 사용자: "ㅇㅇ"
4. (Phase 2B-3) "소스 우선순위 있어? (HuggingFace / arXiv / X / HN 등) 없으면 일반 검색으로 갈게."
5. 사용자: "HN + X 우선"
6. (Phase 2B-4) SKILL.md 작성:

```markdown
---
description: LLM 소식 웹 리서치 후 디스코드 #ai-news에 포스트
---
# /llm-news-digest
## 절차
1. WebSearch로 지난 1시간 새 LLM 소식 검색 (HN / X 우선)
2. 카테고리 분류: 모델 릴리스 / 논문 / 도구
3. 한국어 요약 + 원문 링크
4. mcp__plugin_discord_discord__reply로 #ai-news에 포스트
```

7. (Phase 2B-5 + Phase 3) `jobs.d/-Users-catze-Git-myproject.md` 등록:

```markdown
## llm-news-digest
- prompt: /llm-news-digest
- interval: 1h
- timeout: 10m
- max_per: 5/24h
- notify: failure
```

8. (Phase 5) `heartbeat jobs` 출력 + "1시간마다 1회, 24시간 안에 최대 5회. 다 채우면 자동 skip."

## 금지 사항

- 사용자 동의 없이 `~/.claude/skills/`에 디렉토리/파일 생성 금지.
- 사용자 동의 없이 `~/.claude/heartbeat/jobs.d/` 잡 파일 수정 금지.
- 새 잡을 `~/.claude/HEARTBEAT.md`에 쓰지 않는다(레거시). 거기 이미 있는 이름과도 충돌 확인 —
  파싱은 jobs.d가 이겨서 한쪽이 조용히 무시된다.
- 기존 잡 이름과 충돌하면 사용자에게 확인 ("같은 이름 잡 있어. 덮어쓸래 / 다른 이름?").
- 슬러그가 의심스러우면(예: CWD가 home directory 그대로) 사용자에게 한 번 확인.
- 너무 많은 질문으로 사용자 잡지 말 것 — 합리적 디폴트로 가고, 사용자가 원하면 나중에 잡 파일 직접 편집.
