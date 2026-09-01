---
description: 두 Claude Code 행동 중 틀린 쪽을 그어 로컬 CLAUDE.md 규칙으로 컴파일하고 명시적으로 활성화·롤백한다. 설정 인터뷰 대신 A/B 행동을 비교한다. 사용자가 "xout", "행동 규칙 컴파일", "긋기 세션", "재심(recheck)", "xout 활성화/롤백"을 요청할 때 사용.
argument-hint: "[open|resume|recheck|status|sessions|doctor|validate|enable|rollback|undo]"
disable-model-invocation: true
allowed-tools: 'Bash(python3 *)'
---

# xout - 긋기 세션

xout는 질문 대신 반증 가능한 대비 페어를 제시하고, 사용자의 유일한 동사인
"긋기"만으로 Claude Code 설정(8축 가설 공간 6,561조합)을 수렴시킨다.
세션 런타임에 LLM 호출 0회, 외부 네트워크 호출 0회다
(브라우저와 loopback HTTP 서버 사이의 로컬 통신만 사용).

## 현재 상태

- 착지/재심 현황은 아래 `status` 명령으로 확인한다.

## 실행 규칙

`open`, `resume`, `recheck`, `validate`를 실행하기 전에는 반드시
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" status`를 먼저 실행해
재심 대기 배너를 확인하고, 배너가 있으면 사용자에게 한 줄로 전달한다.

대화형 명령(`open`, `resume`, `recheck`, `validate`)만 **백그라운드로** 실행하고,
로그에 찍힌 `긋기 화면: http://127.0.0.1:<port>/` URL을 사용자에게 알려준다.
서버 없는 조회·진단·소유권 명령(`status`, `sessions`, `doctor`, `enable`,
`rollback`)은 포그라운드로 실행해 종료 코드와 출력을 그대로 전달하며 URL을
기다리지 않는다.

| 인자 | 실행 | 명령 | 설명 |
|---|---|---|---|
| (없음) 또는 `open` | 백그라운드+URL | `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" open --no-browser --repo "$PWD"` | 미완료 일반 세션 1건이면 계속하고, 없으면 새 15긋기 세션 |
| `chat` | 포그라운드 반복 | `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" pair` 로 페어를 받고, 사용자가 고른 탈락 쪽을 `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" strike <left\|right\|both\|pair> --pair-id <id>` 로 기록 | 브라우저 없이 대화 안에서 세션 진행. 페어 두 본문을 그대로 보여주고, 사용자가 명시적으로 고른 것만 기록한다 - 절대 대신 긋지 않는다 |
| `resume <id>` | 백그라운드+URL | `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" resume <id> --no-browser --repo "$PWD"` | 중단 세션 재개 (`id` 생략은 미완료 1건일 때만) |
| `recheck` | 백그라운드+URL | `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" recheck --no-browser --repo "$PWD"` | 4막 경량 재심 |
| `validate` | 백그라운드+URL | `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" validate --no-browser --repo "$PWD"` | 검증 세션 |
| `status` | 포그라운드 출력 | `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" status` | 착지/활성화/재개/판정 요약 |
| `sessions` | 포그라운드 출력 | `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" sessions` | 최근 세션 목록 |
| `doctor` | 포그라운드 출력 | `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" doctor` | 설치·데이터·replay 로컬 진단 |
| `enable` | 포그라운드 출력 | `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" enable --grant` | 명시 요청 시 CLAUDE.md에 소유 @import 한 줄 추가 |
| `rollback` | 포그라운드 출력 | `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" rollback` | xout가 추가한 @import 한 줄만 제거 |

`--repo "$PWD"`는 현재 작업 레포를 읽기 전용으로 스캔해 페어의 기술 문맥만
치환한다. 파일 내용은 읽지 않는다.

## 위 상태 출력에 "재심 대기 N건" 배너가 있으면

세션을 열기 전에 사용자에게 배너 내용을 한 줄로 전달하고, `xout recheck`로
재심에 들어갈 수 있다고 안내한다. 수락 여부는 사용자가 정한다 - 대신 결정하지
않는다.

## 세션이 끝나면

- 산출물은 `~/.claude/xout/` 단독 소유 디렉토리에만 착지한다
  (XOUT.md + manifest.json + settings.xout.json).
- 사용자 CLAUDE.md 활성화는 위 plugin launcher의 `enable --grant`로만 하며
  @import 한 줄만 추가된다. 사용자가 명시적으로 요청할 때만 실행한다.
- 롤백도 위 launcher의 `rollback`만 사용한다 (그 한 줄만 제거).

## 하지 말 것

- 사용자 대신 긋지 않는다 - 긋기는 사용자의 반증 행위다.
- CLAUDE.md나 settings.json을 직접 편집하지 않는다 - xout의 소유권 계약 위반이다.
- 서버 로그의 URL 외 다른 방법으로 세션 상태를 조작하지 않는다.
