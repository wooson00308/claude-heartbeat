---
description: transcript JSONL을 정제하여 메모리 topic 파일과 MEMORY.md 인덱스를 갱신하는 /dream 프로세스. KAIROS autoDream 방식. "/dream" 입력 시 트리거.
---

# /dream — transcript 정제 프로세스

## 개요

클로드 코드가 자동 저장하는 transcript JSONL을 정제하여 메모리 topic 파일로 변환하고 MEMORY.md 인덱스를 갱신한다.

파이프라인:
```
JSONL (수 MB) → 파이썬 전처리 (경량 마크다운) → 본체가 읽고 판단 (메모리 갱신)
```

전처리 스크립트(`dream-prep`)가 도구 호출 합치기, 코드 블록 압축, 시스템 메시지 제거 등 기계적 처리를 담당.
본체는 전처리된 마크다운만 읽고 메모리 갱신 판단에 집중.

## 경로 규칙

- transcript 원본: `~/.claude/projects/{slug}/*.jsonl`
- 전처리 출력: `~/.claude/projects/{slug}/memory/_dream_prep/*.md`
- 메모리: `~/.claude/projects/{slug}/memory/`
- 인덱스: `~/.claude/projects/{slug}/memory/MEMORY.md`
- 메타: `~/.claude/projects/{slug}/memory/dream_meta.md`
- project-slug: CWD 경로의 `/` → `-` 변환. 예) `/Users/yourname` → `-Users-yourname`

## 실행 프로세스

### Phase 1: Orient

1. 현재 프로젝트의 slug 확인 (CWD 기반)
2. MEMORY.md 읽어서 기존 topic 파일 목록 파악
3. dream_meta.md에서 마지막 정제 시점과 처리된 transcript 목록 확인
4. `dream-prep status --slug="{slug}"` 로 미처리 transcript 수 확인
5. 사용자에게 보고: "N개의 미처리 transcript 발견"

### Phase 2: Gather (전처리 스크립트)

transcript JSONL을 직접 읽지 않는다. 전처리 스크립트가 기계적으로 처리한다.

1. Bash로 실행: `dream-prep prep --slug="{slug}" -n 5`
2. 스크립트가 수행하는 처리:
   - JSONL에서 유저 텍스트 + 어시스턴트 텍스트만 추출
   - 시스템 메시지 (`<` 시작) 제거
   - 연속 도구 호출 한 줄로 합치기 (`[도구: Bash, Read x2]`)
   - 코드 블록 3줄 이하 유지, 4줄 이상 첫 줄 + `... (N줄 생략)`
   - 유저 메시지 3자 이하 제거
   - compact 경계 감지: `"This session is being continued from a previous conversation"`으로 시작하는 user 메시지를 자연 청크 경계로 활용
   - 활성/거대 transcript 게이트: mtime quiet 30분 미만이면 활성 파일로 간주. 단, 파일 크기 10MB 이상인 경우 mtime quiet 30분 조건 무시하고 강제 처리. 활성 파일 처리 시 라운드 윈도우 = stat() 시점의 파일 크기(H) + 마지막 leafUuid 캡처. H 이후 append는 다음 라운드 처리.
3. 결과: `memory/_dream_prep/prep_{timestamp}.md`
4. 이 파일을 Read로 읽기
5. 마킹 책임은 CLI에 있음: dream-prep은 prep 산출물 생성 직후 자동으로 `dream-prep mark`를 실행하여 dream_meta.md의 `processed_v2:` 섹션에 `(file, last_uuid)` 마킹을 박는다. LLM은 prep 결과만 읽고 처리하며, 마킹은 CLI가 보장한다.

### Phase 3: Consolidate

전처리 파일을 읽고 기억할 가치가 있는 정보를 식별한다.

1. 분류 기준:
   - 사용자 프로필/선호도 변경 → user 타입
   - 작업 방식 피드백/교정 → feedback 타입
   - 프로젝트 상태/결정/일정 → project 타입
   - 외부 리소스 참조 → reference 타입
2. 기존 topic 파일과 대조:
   - 이미 있는 내용 → 스킵
   - 기존과 모순 → 최신 정보로 갱신 (Edit)
   - 새로운 내용 → 기존 topic에 추가 or 새 topic 생성 (Write)
3. 상대 날짜 → 절대 날짜 변환
4. 메모리 frontmatter 규칙:
   ```
   ---
   name: {memory name}
   description: {한 줄 설명}
   type: {user|feedback|project|reference}
   ---
   ```
5. feedback: "규칙 → Why → How to apply" 구조
6. project: "사실/결정 → Why → How to apply" 구조

### Phase 4: Prune & Index

1. MEMORY.md 인덱스 재구성:
   - 실제 파일 없는 포인터 제거
   - 새 topic 파일 포인터 추가
   - 200줄 이하, 라인당 ~150자
   - 형식: `- [파일명.md](파일명.md) — 한 줄 설명`
2. dream_meta.md 갱신 (LLM 담당 범위):
   - last_dream 타임스탬프만 업데이트
   - last_lint 타임스탬프만 업데이트 (Phase 5 완료 시)
   - `processed:` (legacy) 및 `processed_v2:` 섹션은 절대 건드리지 않는다. 두 섹션의 마킹 책임은 전적으로 dream-prep CLI에 있다. LLM이 마킹 라인을 확인하고 싶으면 dream-prep CLI 출력을 참조할 것.
3. _dream_prep/ 디렉토리 정리 (처리 완료된 prep 파일 삭제)
4. 결과 보고: 생성/갱신/삭제된 topic 파일 목록

### Phase 5: Lint

처리 결과 정합성 검사.

1. Check 1 — 고아 감지:
   - MEMORY.md에 포인터가 있으나 실제 파일이 없는 항목 → 경고
   - legacy `processed:` 라인과 `processed_v2:` 항목에 동일 파일이 양쪽 다 있는 경우 → 정상 (마이그레이션 진행 중). lint는 경고만 출력하고 자동 제거하지 않는다.
2. Check 2 — 중복 포인터: MEMORY.md에 동일 파일명이 두 번 이상 등장 → 하나 제거
3. last_lint 타임스탬프 갱신 (dream_meta.md)

## 주의사항

- transcript 원본은 절대 수정/삭제하지 않는다
- topic 파일 쓰기 성공 후에만 MEMORY.md 인덱스 업데이트 (Strict Write Discipline)
- 인사이트급 발견은 문샤인에 별도 등록
  - 메모리: "매번 필요한 맥락" (프로필, 피드백, 프로젝트 상태)
  - 문샤인: "필요할 때 꺼내 쓰는 지식" (인사이트, 디버깅 기록, 시행착오)
- 한 번에 5개씩 처리. 미처리가 많으면 여러 라운드로 나눠서 실행
- 모든 내용은 한국어로 작성

## 비침습 모딩 원칙

claude-heartbeat + /dream은 Claude 메모리/세션 기능을 "확장"하되 "간섭"하지 않는 모딩 위치다.

- transcript 원본 (*.jsonl)은 read-only로만 접근. 어떤 경우에도 flock을 잡지 않는다.
- Claude Code가 대화를 기록 중인 파일과 동시 접근 시 read-during-write 안전 보장: O_RDONLY + 라운드 윈도우 동결(H 기준) 조합으로 처리.
- dream_meta.md 수정은 atomic rename + fcntl 락으로만 수행. race condition 없음.
- 기존 메모리 파일 (`~/.claude/projects/{slug}/memory/*.md`) 동작에 영향 0. dream이 쓰는 파일은 dream이 생성한 topic 파일과 MEMORY.md 인덱스뿐.
- Claude 메모리/세션 내부 메커니즘(CLAUDE.md 로딩, 시스템 프롬프트 주입 등)은 건드리지 않는다.

## 메타파일 포맷

`dream_meta.md`의 현행 포맷 (v2):

```yaml
---
name: dream_meta
description: /dream 프로세스 메타데이터
type: reference
---
last_dream: 2026-05-11T10:30:00
last_lint: 2026-05-11T10:30:00

processed:
- xxx.jsonl       # legacy (구파서 호환용, 신규 코드에선 안 건드림)
- yyy.jsonl

processed_v2:
- file: aaaaa.jsonl
  last_uuid: 2d040eac-...
- file: bbbbb.jsonl
  last_uuid: f02ab3e4-...
  status: sealed   # 선택. sealed(완료) | active(부분처리 중)
```

섹션별 역할:

- `last_dream` / `last_lint`: LLM이 갱신하는 유일한 필드. 타임스탬프만.
- `processed:` (legacy): 구파서 호환용 + 마이그레이션 안전망. 신규 코드에서는 읽기만 하고 쓰지 않는다. 두 섹션을 합쳐서 "처리된 파일 전체" 판단.
- `processed_v2:`: dream-prep CLI가 단독으로 관리. `dream-prep mark` 명령이 fcntl 락 + atomic rename으로 기록. `last_uuid`는 해당 라운드 윈도우의 마지막 leafUuid. `status: active`는 대용량 파일 부분 처리 중임을 의미.
- 동시성 보장: dream_meta.md 쓰기는 fcntl 락 + atomic rename 조합으로 race condition 제거.

## 저장하지 않는 것

- 코드 패턴, 아키텍처, 파일 경로 — 코드에서 직접 확인 가능
- git 히스토리 — git log/blame으로 확인 가능
- 디버깅 솔루션 — 코드와 커밋 메시지에 있음
- CLAUDE.md에 이미 문서화된 내용
- 의미 없는 단답 ("ㅇㅇ", "ㄱㄱ", "ㅎㅇ" 등) — 맥락 없이는 가치 없음
- 단, 성격/취향/감정이 드러나는 잡담은 user 타입으로 저장할 것 (예: 좋아하는 것, 정 드는 성향, 유머 스타일 등)
