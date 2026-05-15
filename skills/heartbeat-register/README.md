# heartbeat-register skill

_Turn natural-language requests into HEARTBEAT.md jobs._

---

자연어 한 줄로 heartbeat 잡을 등록한다. 단순 명령이면 prompt 직접 박고, 복잡한 multi-step 요청이면 사용자 동의 받고 새 SKILL.md를 생성해서 잡과 페어로 정착시킨다.

## Why

`prompt: ...`에 긴 자연어를 박는 건 매번 결과가 들쭉날쭉해진다. claude-heartbeat의 정석은 "복잡한 작업 = 스킬로 정착, prompt는 `/skill-name`만 박기" (dream 스킬이 그 패턴).

근데 사용자가 매번 직접 SKILL.md를 손으로 짜는 건 부담이라 — 이 등록 스킬이 자연어 요청을 받아 분류하고, 필요하면 동의 받고 SKILL.md 페어를 자동 생성한다.

## Install

```bash
heartbeat install heartbeat-register
```

이건 `~/.claude/skills/heartbeat-register/SKILL.md`만 복사한다. 등록 스킬 자신은 heartbeat 잡으로 등록되지 않는다.

## Usage

Claude Code 세션에서 자연어로:

```
/heartbeat-register

# 또는

이 작업 하트비트에 등록해줘 — 매일 오전 9시에 git log 요약
```

또는 더 복잡한 요청:

```
하루 5번 1시간 간격으로 LLM 소식 웹 리서치하고 디스코드 #ai-news에 포스트해줘
```

Claude가 분류하고, 복잡 분기면 동의 받고 SKILL.md를 만든다. 절차는 `SKILL.md` 참고.

## 안전 원칙

이 스킬은 사용자 명시적 동의 없이 다음을 하지 않는다:

- `~/.claude/skills/`에 새 SKILL.md 생성
- `~/.claude/HEARTBEAT.md` 수정

갑자기 모르는 스킬이 늘어나거나 잡이 등록되는 일은 없다.
