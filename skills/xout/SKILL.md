---
name: xout
description: X out the AI behavior you never want - compare two concrete agent behaviors, cross out the wrong one, and compile the survivors into local CLAUDE.md rules with explicit activation and rollback. 두 Claude Code 행동 중 아닌 쪽에 X를 쳐서 로컬 CLAUDE.md 규칙으로 컴파일하고 명시적으로 활성화·롤백한다. 사용자가 "xout", "행동 규칙 컴파일", "긋기 세션", "재심(recheck)", "xout 활성화/롤백"을 요청할 때 사용.
argument-hint: "[chat|status|sessions|doctor|enable|rollback|undo]"
disable-model-invocation: true
allowed-tools: 'Bash(python3 *)'
---

# xout - X 세션

xout은 질문 대신 반증 가능한 대비 페어를 제시하고, 사용자의 유일한 동사인
"X 치기"만으로 Claude Code 설정(8축 가설 공간 6,561조합)을 수렴시킨다.
세션 런타임에 LLM 호출 0회, 외부 네트워크 호출 0회다 - 모든 진행은 로컬
append-only 이벤트 원장 위에서 일어난다.

## 실행 규칙

어떤 세션이든 시작 전에 반드시
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" status`를 먼저 실행해
재심 대기 배너를 확인하고, 배너가 있으면 사용자에게 한 줄로 전달한다.

세션은 **대화형(chat) 모드**로 진행한다:

1. `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" pair`를 실행해
   JSON을 받는다. `pair.left_text` / `pair.right_text` / `pair.pair_id`가 핵심이다.
2. 두 행동 본문을 **그대로** 사용자에게 보여주고, 어느 쪽에 X를 칠지 묻는다.
3. 사용자가 명시적으로 고른 것만
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" strike <left|right|both|pair> --pair-id <pair_id>`로 기록한다.
4. 응답 JSON의 `session_complete`가 true가 될 때까지 1-3을 반복한다.
5. 완료되면 `rules`의 8줄을 보여주고 적용 여부를 묻는다. 예라고 할 때만
   `enable --grant`를 실행한다.

사용자가 터미널에서 직접 하고 싶어하면 `xout`(또는 `uvx xout`) 한 줄을
안내한다 - 같은 원장이라 어디서 하다 멈춰도 이어진다.

| 인자 | 실행 | 명령 | 설명 |
|---|---|---|---|
| (없음) 또는 `chat` | 포그라운드 반복 | 위 대화형 루프 (`pair` -> 사용자 선택 -> `strike`) | 미완료 세션 1건이면 이어서, 없으면 새 15긋기 세션 |
| `status` | 포그라운드 출력 | `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" status` | 착지/활성화/재개/판정 요약 |
| `sessions` | 포그라운드 출력 | `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" sessions` | 최근 세션 목록 |
| `doctor` | 포그라운드 출력 | `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" doctor` | 설치·데이터·replay 로컬 진단 |
| `enable` | 포그라운드 출력 | `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" enable --grant` | 명시 요청 시 CLAUDE.md에 소유 @import 한 줄 추가 |
| `undo` 또는 `rollback` | 포그라운드 출력 | `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" undo` | xout이 추가한 @import 한 줄만 제거 |

## 위 상태 출력에 "재심 대기 N건" 배너가 있으면

세션을 열기 전에 사용자에게 배너 내용을 한 줄로 전달하고, 터미널에서
`xout recheck`로 재심에 들어갈 수 있다고 안내한다. 수락 여부는 사용자가
정한다 - 대신 결정하지 않는다.

## 세션이 끝나면

- 산출물은 `~/.claude/xout/` 단독 소유 디렉토리에만 착지한다
  (XOUT.md + manifest.json + settings.xout.json).
- 사용자 CLAUDE.md 활성화는 위 launcher의 `enable --grant`로만 하며
  @import 한 줄만 추가된다. 사용자가 명시적으로 동의할 때만 실행한다.
- 롤백도 위 launcher의 `undo`만 사용한다 (그 한 줄만 제거).

## 하지 말 것

- 사용자 대신 X를 치지 않는다 - X는 사용자의 반증 행위다.
- CLAUDE.md나 settings.json을 직접 편집하지 않는다 - xout의 소유권 계약 위반이다.
- 페어 본문을 요약하거나 편집해 보여주지 않는다 - 원문 그대로가 측정 도구다.
