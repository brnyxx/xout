# Popper 리브랜딩 기획서 (Draft v2)

> **Phase 0 결정 (2026-09-01): 새 이름 = `xout`.**
> 캐치프레이즈: "X it out." / "X out the AI behavior you never want again."
> 네임스페이스 확인 (2026-09-01 조회): PyPI `xout` 미등록(404, 사용 가능). npm `xout` 선점(200, Python 프로젝트라 비치명). GitHub 유저/org `xout` 존재(200) - 리포는 `brnyxx/xout`으로 사용하므로 무관. 도메인(xout.dev 등)은 RDAP 응답이 불확실하여 미확인 `[교차검증 필요: 등록 전 레지스트라에서 직접 조회]`.

> 목표: "설명이 필요한 도구"에서 "3초 만에 이해되고, 한 줄로 설치되고, 한 번 해보고 싶어지는 도구"로.
> paperthin / ouroboros 급의 바이럴 가능 구조로 브랜드, 메시징, UX를 재설계한다.
> 엔진(이벤트 원장, 원자적 랜딩, 롤백 소유권, 8축 카탈로그)은 유지한다. 바꾸는 것은 껍데기와 흐름이다.

---

## 1. 진단: 왜 지금 Popper는 바이럴이 안 되는가

| # | 문제 | 현재 상태 | 바이럴 관점 판정 |
|---|---|---|---|
| 1 | 이름이 설명을 요구함 | "Popper" = 칼 포퍼 반증주의. 철학 배경지식 없으면 감이 안 옴 | 이름을 듣고 기능을 상상할 수 없음 |
| 2 | 한 줄 정의가 어려움 | "A local CLAUDE.md behavior compiler" | "컴파일러"라는 단어에서 타겟 유저 절반이 이탈 |
| 3 | 설치가 3단계+ | venv 생성 -> wheel URL pip install -> doctor -> open. 플러그인 경로는 체크섬 3파일 검증까지 | 한 줄 설치(uvx/npx) 시대에 치명적 |
| 4 | 개념 어휘가 논문급 | 긋기, 반증, 6561 카운터, mined-prior, evidence grade, 봉인, 4막 재심, 13 discriminative slots | 신뢰성엔 기여하지만 첫인상에서는 진입장벽 |
| 5 | 표면 명령어 11개 | open / resume / sessions / doctor / status / recheck / validate / enable / rollback / export / data | "이것만 하세요"가 불가능한 명령어 수 |
| 6 | 진입 장벽 프레이밍 | "15회 긋기" - 왜 15번인지 스스로 설명해야 함 | 횟수가 아니라 소요 시간(2분)으로 팔아야 함 |
| 7 | README가 방어 우선 | prereg, axis locality, "not a guarantee" 등 방어적 서술이 상단부터 등장 | 정직함은 자산이지만 배치가 첫인상을 죽임 |
| 8 | 세션 UI가 한국어 전용 | 영어 런타임 팩 미출시 (README에 명시) | 글로벌 바이럴 원천 차단 |

핵심 요약: **제품력(안전 계약, 로컬 온리, 복구 보장)은 이미 상위권인데, 그 위에 "학술 포장"이 덮여 있다.**

## 2. 벤치마크: 바이럴 프로젝트의 공통 공식

paperthin, ouroboros류 프로젝트가 공유하는 패턴:

1. **이름 = 컨셉**: 이름만 들어도 이미지가 떠오른다 (paperthin = 얇음, ouroboros = 자기 순환).
2. **한 줄 설치**: `uvx foo` 또는 `npx foo`. 복붙 한 번, 실패 지점 0개.
3. **3초 hero**: GIF 한 장 + 문장 한 줄로 전체 제품이 이해됨.
4. **단일 동사 UX**: 사용자가 기억할 행동이 하나 ("긋는다" / "스와이프한다").
5. **밈이 될 수 있는 문장**: 트윗/스레드에 그대로 복붙되는 캐치프레이즈.
6. **정직한 축소**: 기능 나열 대신 "이것 하나를 한다"를 반복.

## 3. 리브랜딩 핵심: 인터랙션을 브랜드로 만든다

Popper의 진짜 자산은 철학이 아니라 **제스처**다:

> **두 개의 에이전트 행동을 보여준다. 싫은 쪽을 긋는다. 살아남은 쪽이 규칙이 된다.**

이 제스처는 전 세계인이 이미 아는 메타포가 있다: **데이팅 앱 스와이프**.
"Tinder for your coding agent"는 설명 0초짜리 포지셔닝이다.

### 3.1 네이밍 후보 (PyPI 조회 결과 포함)

PyPI 조회는 2026-09-01 `https://pypi.org/pypi/<name>/json` HTTP 상태코드로 확인 (404 = 미등록).
GitHub org/repo명, npm, 도메인, 상표는 별도 확인 필요 `[교차검증 필요]`.

| 안 | 이름 | PyPI | 캐치프레이즈 | 강점 | 리스크 |
|---|---|---|---|---|---|
| A (추천) | **swipeleft** | 미등록 (404) | "Swipe left on bad AI behavior." / "당신의 AI에게 왼쪽 스와이프를." | 메타포 자체가 글로벌 공용어. UI를 스와이프 카드로 바꾸면 이름=제스처=제품이 일치. 데모 GIF가 저절로 밈이 됨 | UI 리워크 필요(중). 데이팅 앱 연상이 싫은 유저 존재 |
| B | **strikethrough** | 미등록 (404) | "~~Never again.~~ Cross it out." | 기존 긋기 UX 100% 유지, 리워크 최소. 이름이 곧 화면에 보이는 결과물(취소선) | 이름이 김. 명령어로 치기에 길다 (alias 필요) |
| C | **dealbreaker** | 미등록 (404) | "15 dealbreakers. One better agent." | "이건 못 참지" 정서를 정확히 표현. 발음/기억 용이 | 제스처가 이름에 없음. 설명 한 줄은 여전히 필요 |
| D | **the-ick** / icklist | 미등록 (404) | "Give your agent the ick list." | 밈 파워 최상, Gen-Z 훅 | 밈 수명 리스크. 몇 년 뒤 촌스러워질 수 있음 |

기존 후보 중 nope, crossout, redpen, veto, unlearn, nitpick, ick는 PyPI 선점됨 (200).

**확정 (2차 라운드): `xout`.**
1차 후보(swipeleft 등)와 2차 확장 후보 25개(PyPI 조회 포함: hardpass, ruleout, crossoff, strikeit, nixit 등 미등록 / nah, nix, redline, weedout 등 선점) 중에서 xout으로 결정.
이유: (1) 이름 = 동작 = 로고(X 하나)가 전부 일치, (2) 두 음절이라 명령어로 치는 맛이 있음(`uvx xout`), (3) 기존 긋기(취소선/X표) UI를 그대로 계승하므로 스와이프 UI 리워크가 불필요해짐.

### 3.2 메시징 시스템 (xout 확정안)

- **한 줄 정의 (EN)**: "Your agent shows two ways it could behave. X out the wrong one. The rest becomes its rules."
- **한 줄 정의 (KO)**: "AI의 두 행동 중 아닌 쪽에 X를 치세요. 남은 쪽이 규칙이 됩니다."
- **캐치프레이즈 (짧은 형)**: "X it out."
- **캐치프레이즈 (긴 형)**: "X out the AI behavior you never want again."
- **서브 카피 (신뢰 한 줄)**: "No cloud. No telemetry. No LLM calls. One-line rollback."
- **숫자 프레이밍**: "6,561 combinations" 대신 -> **"2 minutes. 8 rules."** (2분, 규칙 8줄)
- **로고 방향**: X 글리프 하나. 붉은 X가 그어지는 마이크로 애니메이션이 곧 브랜드 모션.

### 3.3 새 README 구조 (다운 받고 > 이것만 하세요)

```
[로고 + GIF: 행동 카드에 붉은 X가 그어지며 사라지는 6초 루프]

# xout
X out the AI behavior you never want again.

    uvx xout

That's it. Browser opens. X things out for 2 minutes. Your agent gets 8 rules.

1. X out the behavior you hate
2. xout writes the surviving choices as rules
3. Claude Code loads them - undo anytime with `xout undo`

No cloud. No telemetry. No LLM calls. Everything stays in ~/.claude.

[▼ details 접기: 8축 설명 / 안전 계약 / 플러그인 설치 / 개발자 문서 링크]
```

원칙: 스크롤 첫 화면 안에 설치 명령과 3-step이 전부 들어간다. 현재 README의 방어적/학술적 서술(6561, prereg, evidence grade, mined-prior)은 전부 접힌 섹션 또는 docs/로 강등하되 삭제하지 않는다.

## 4. UX/기능 기획 수정 (직관화)

### 4.1 설치: 한 줄로

| 현재 | 변경 |
|---|---|
| venv + GitHub release wheel URL pip install (2~4줄) | `uvx xout` 한 줄 (PyPI 정식 배포 전제) `[교차검증 필요: uvx가 PyPI 패키지의 console script를 실행하는 표준 흐름인지 uv 공식 문서로 확인 후 문서화]` |
| 플러그인: zip + SHA256SUMS + verify 스크립트 3파일 수동 검증 | `claude plugin install` 가능한 공식 마켓플레이스 등록을 1순위 경로로. 체크섬 검증 경로는 "보안 강화 설치"로 접어서 유지 |

### 4.2 표면 명령어: 11개 -> 3개

| 새 명령 | 하는 일 | 흡수되는 기존 명령 |
|---|---|---|
| `xout` (인자 없음) | 세션 시작. 미완료 세션 있으면 자동 이어하기. 시작 전에 doctor 체크를 내부 수행, 문제 있을 때만 표시 | open, resume, doctor |
| `xout undo` | 적용 취소 (기존 rollback) | rollback |
| `xout status` | 현재 규칙 8줄 + 적용 여부 표시 | status, sessions |
| `xout dev ...` (숨김) | recheck / validate / export / data / sessions --json 등 파워유저 기능 전부 이동 | 나머지 전부 |

기존 명령은 deprecation alias로 1개 버전 유지 후 제거.

### 4.3 적용 흐름: enable 단계 제거 (동의는 유지)

- 현재: 세션 완료 -> 아무 일도 안 일어남 -> 사용자가 `popper enable --grant`를 알아내서 실행.
- 변경: 15번째(또는 최종) X 직후 결과 화면에서 **"지금 적용할까요? [적용] [나중에]"** 를 바로 묻는다. [적용] = 기존 enable --grant와 동일한 동의 + 동일한 receipt-owned import 1줄. 명시적 동의라는 안전 계약은 그대로, 단계 수만 3 -> 1.

### 4.4 진행 표현: 6561 카운터 폐기

- 6561 카운트다운 -> **"규칙 8개 중 N개 확정"** 진행바 + 확정될 때마다 규칙 카드가 뒤집히는 연출.
- 근거였던 "탐색 공간 시각화"는 결과 화면 하단 한 줄로 강등: "you narrowed 6,561 possible agents down to 1."  (이 문장이 오히려 공유용 카피가 됨)

### 4.5 용어 교체 맵 (UI 노출 어휘만, 내부/문서 용어는 유지)

| 현재 (UI 노출) | 변경 (KO) | 변경 (EN) |
|---|---|---|
| 긋기 | X 치기 (붉은 X 애니메이션 + 취소선 연출로 계승) | x out |
| 반증 / falsified | 탈락 | x'd out |
| survivor / 생존 | 선택됨 | kept |
| mined-prior | 추정 기본값 (아직 안 물어봄) | guessed default (not asked yet) |
| evidence grade | 확실함 표시 (확정 / 추정) | confirmed / guessed |
| 재심 (recheck) | 다시 고르기 | re-pick |
| 봉인 / sealed | (UI에서 미노출, 내부 유지) | - |

원칙: **정직성 계약은 유지한다.** "guessed default"는 mined-prior의 정직한 일상어 번역이지 은폐가 아니다. manifest.json의 필드명 등 기계 계약은 변경하지 않는다.

### 4.6 세션 길이 프레이밍

- 1순위: 횟수 대신 시간으로 표기 ("2분"). 15회는 유지.
- 2순위(검토): 15 -> 10회 축소. 단, 8축 커버리지/판별력 제약이 scoring, fixtures 봉인 로직과 얽혀 있으므로 **엔진 영향 분석 후 별도 결정** (이번 리브랜딩 범위에서 기본값은 15 유지).

### 4.7 결과 화면 리디자인

- 최종 화면 = "당신의 에이전트 규칙 8줄" 카드 리스트 + [적용] 버튼 + [복사] 버튼 + 공유용 요약 이미지(로컬 생성, 업로드 없음).
- 확정/추정 뱃지로 evidence grade를 시각화.

### 4.8 영어 런타임 팩 (바이럴 전제조건)

- 현재 세션 UI와 생성 규칙 텍스트가 한국어 전용. 글로벌 확산에는 EN 팩이 필수.
- 규칙 텍스트는 축별 고정 카탈로그이므로 번역 대상이 유한함 (8축 x 3값 + UI 문자열).
- i18n 규칙 적용: 하드코딩 문자열을 키 기반으로 분리, ko를 source of truth로 en 추가.

### 4.9 버리지 않는 것 (신뢰 자산 -> 셀링 포인트로 압축)

아래는 전부 유지하되, 마케팅 문장 한 줄로 압축해 노출한다:

- 로컬 온리 / 무 LLM 호출 / 무 텔레메트리 -> "No cloud. No telemetry."
- append-only 원장, 원자적 랜딩, 크래시 복구, 중복 세션 거부 -> "Crash-safe. Resume anytime."
- receipt-owned import 1줄 + 롤백 -> "One-line rollback."
- 8축 동결 카탈로그, prereg, 정직한 evidence 라벨 -> 접힌 "How it's honest" 섹션

## 5. 스코프 가드 (이번에 하지 않는 것)

- 엔진 재작성 없음 (events / writer / recovery / conflict / scoring 로직 불변).
- 8축 카탈로그 동결 유지, 새 축 추가 없음.
- 안전 계약(동의, 롤백 소유권, 무 텔레메트리) 완화 없음.
- 클라우드 프로필, 모델 평가, 오케스트레이션으로의 확장 없음.

## 6. Phase 제안 (논의용 초안)

| Phase | 이름 | 내용 | 산출물 | 코드 변경 | 선행 결정 |
|---|---|---|---|---|---|
| 0 | 브랜드 확정 | 이름/캐치프레이즈/추천안 결정, GitHub·PyPI·npm 최종 가용성 확인 | 확정 브랜드 1개 | 없음 | 사용자와 이 문서 논의 |
| 1 | 메시징 리브랜딩 | README(en/ko), Pages 사이트, 로고/hero/GIF 재제작, 리포 rename + redirect | 새 README, 새 사이트 | 문서/에셋만 | Phase 0 |
| 2 | 설치 한 줄화 | PyPI 정식 배포(패키지명 변경), uvx 경로 검증, 플러그인 마켓플레이스 1순위 경로 정리 | `uvx xout` 동작 | 패키징 설정 | Phase 0 (이름) |
| 3 | UX 직관화 | 명령어 3개 축소(+alias), 완료 즉시 적용 프롬프트, 6561 카운터 교체, 용어 교체, 결과 화면 | v2.0 UX | CLI + web UI | Phase 0~1 |
| 4 | X 모션 + EN 팩 | 붉은 X 긋기 애니메이션 강화(스와이프 리워크는 xout 확정으로 제거), i18n 분리 + 영어 런타임 팩 | 글로벌 데모 GIF | web UI + i18n | Phase 3 |
| 5 | 런치 | v2.0 릴리스, 데모 GIF/공유 이미지, Show HN·트위터 스레드용 카피 | 릴리스 + 런치 킷 | 릴리스 작업 | Phase 1~4 |

의존성: 0 -> 1 -> (2 ∥ 3) -> 4 -> 5. Phase 2와 3은 병렬 가능.

## 7. 열린 질문 (사용자 결정 필요)

1. ~~**이름**~~: **xout으로 확정 (2026-09-01)**.
2. ~~**스와이프 UI 전환**~~: xout 확정으로 해소 - 기존 긋기(X표) UI를 계승하고 붉은 X 애니메이션만 강화. Phase 4에서 스와이프 리워크 항목 제거.
3. **리포 rename** (brnyxx/popper -> brnyxx/xout) 진행 여부와 시점 - 기존 v1.3.1 릴리스 URL 호환성 처리 포함.
4. **15회 유지 vs 10회 축소** - 축소 시 엔진 영향 분석 선행.
5. **PyPI 배포** - 계정/네임스페이스 확보를 Phase 0에서 바로 할지.
6. **한국어 v1 정체성** - EN 팩 이후에도 "Korean-first" 브랜딩을 유지할지, 완전 이중언어로 갈지.

---

*작성: 2026-09-01. PyPI 가용성은 작성 시점 조회 결과이며 등록 전 재확인 필요.*
