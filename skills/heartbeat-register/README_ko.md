# heartbeat-register 스킬

_자연어 요청을 HEARTBEAT.md 잡으로 바꿔줍니다._

**[English](README.md)**

---

자연어 한 줄로 heartbeat 잡을 등록합니다. 단순 명령은 prompt에 바로 박고, 복잡한 멀티스텝 요청은 사용자 동의를 받은 뒤 새 SKILL.md를 만들어 잡과 페어로 정착시킵니다.

## 왜?

`prompt: ...`에 긴 자연어를 박는 건 매번 결과가 들쭉날쭉해집니다. claude-heartbeat의 정석은 "복잡한 작업 = 스킬로 정착, prompt는 `/skill-name`만 박기" 입니다 (dream 스킬이 그 패턴).

다만 사용자가 매번 직접 SKILL.md를 손으로 짜는 건 부담이라, 이 등록 스킬이 자연어 요청을 받아 분류하고 필요할 때만 동의 받고 SKILL.md 페어를 자동 생성합니다.

## 설치

```bash
heartbeat install heartbeat-register
```

이 명령은 `~/.claude/skills/heartbeat-register/SKILL.md`만 복사합니다. 등록 스킬 자신은 heartbeat 잡으로 등록되지 않습니다.

## 사용

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

Claude가 자동으로 분류하고, 복잡한 분기면 사용자 동의를 받은 뒤 SKILL.md를 만듭니다. 절차는 `SKILL.md`를 참고하세요.

## 안전 원칙

이 스킬은 사용자의 명시적 동의 없이 다음을 하지 않습니다:

- `~/.claude/skills/` 아래에 새 SKILL.md 생성
- `~/.claude/HEARTBEAT.md` 수정

모르는 스킬이 갑자기 늘어나거나 잡이 등록되는 일은 없습니다.
