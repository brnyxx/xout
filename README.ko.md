<div align="center">

<h1>xout</h1>

<img src=".github/assets/logo.svg" alt="xout 로고: 굵은 크림슨 X로 지워진 행동 카드와 그 아래 남은 규칙 한 줄" width="96">

**다시 보고 싶지 않은 AI 행동에 X를 치세요.**

<img src=".github/assets/hero.svg" alt="버그 고쳐줘가 A/B 행동 테스트가 되는 그림: 시작할까요?에 X가 그어지고, 고쳤고 테스트 통과가 살아남아 먼저 실행하고 나중에 보고한다는 규칙이 된다" width="920">

[![PyPI](https://img.shields.io/pypi/v/xout)](https://pypi.org/project/xout/) [![CI](https://github.com/brnyxx/xout/actions/workflows/ci.yml/badge.svg)](https://github.com/brnyxx/xout/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[English](README.md) · [소개 사이트](https://brnyxx.github.io/xout/)

</div>

코딩 에이전트가 행동할 수 있는 두 가지 방식을 보여줍니다. 아닌 쪽에 X를 치세요. 2분, 15번의 X 후에 살아남은 선택들이 Claude Code가 `CLAUDE.md`에서 읽는 로컬 규칙 8줄로 컴파일됩니다.

```bash
uvx xout
```

이게 전부입니다. 세션은 터미널 안에서 그대로 진행되고, 2분 정도 X를 치면 에이전트에게 규칙 8줄이 생깁니다.

<img src=".github/assets/demo.gif" alt="실제 xout 터미널 세션이 15회 X를 진행해 조건부 규칙 8줄을 컴파일하고 한 번의 입력으로 적용하는 모습" width="860">

**클라우드 없음. 텔레메트리 없음. LLM 호출 없음. 롤백은 한 줄.**

**v1.0.0 · Python 3.10–3.14 · MIT · 서드파티 런타임 패키지 0개**

<details>
<summary><strong>다른 설치 경로</strong> (pip, venv)</summary>

```bash
pip install xout
xout
```

완전 격리를 원하면:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install xout
.venv/bin/xout
```

Popper 1.x에서 올라오는 경우? `xout`을 한 번 실행하면 됩니다: `~/.claude/popper/`의 데이터가 `~/.claude/xout/`으로 이동하고, 소유된 import 한 줄은 xout이 직접 썼다고 증명 가능할 때만 갱신됩니다.

</details>

## 동작 방식

1. **싫은 행동에 X를 칩니다.** 매 페어는 실제 에이전트 행동 둘을 비교합니다: 먼저 물어보기 vs 먼저 실행하기, 요청 범위 엄수 vs 적극적 정리, 테스트 먼저 vs 테스트 나중 같은 것들.
2. **xout이 컴파일합니다.** 살아남은 선택들이 실행 가능한 규칙 8줄이 되어 근거와 출처를 담은 채 `~/.claude/xout/`에 원자적으로 기록됩니다.
3. **키 입력 한 번으로 적용합니다.** 완료 화면에서 "지금 적용할까요?"를 물으며, 예라고 하면 `~/.claude/CLAUDE.md`에 xout 소유의 `@import` 한 줄만 추가됩니다. `xout undo`가 그 한 줄만 제거합니다.

새 Claude Code 세션에서 예전에 거슬리던 요청을 다시 해보고 규칙이 지켜지는지 확인하세요. 규칙이 낡았다 싶으면 다시 `xout`.

## 얻는 것

15번째 X 이후 `~/.claude/xout/`에 세 파일이 착지합니다:

| 파일 | 내용 |
|---|---|
| `XOUT.md` | Claude Code용 실행 가능한 규칙 8줄 |
| `manifest.json` | 규칙 값, 확신 라벨, 출처, 콘텐츠 해시 |
| `settings.xout.json` | 검토 가능한 설정 제안 |

X로 직접 확정한 규칙은 **확정**, xout이 묻지 않고 추정한 기본값은 정직하게 **추정**으로 표시되고 다시 고르기 대기열에 들어갑니다. 명시적 동의 없이 활성화되는 것은 없습니다.

*(Popper 1.x에서는 같은 파일이 `POPPER.md`, `settings.popper.json`으로 `~/.claude/popper/`에 착지했습니다. xout이 첫 실행 시 자동으로 마이그레이션합니다.)*

## 명령어

| 명령 | 하는 일 |
|---|---|
| `xout` | 세션 시작(미완료 세션은 자동 재개), 시작 전 자가 점검 |
| `xout undo` | xout이 소유한 import 한 줄 제거 - 완전한 롤백 |
| `xout status` | 규칙 8줄과 활성화 여부 표시 |
| `xout dev ...` | 파워 도구: export, validate, 다시 고르기, 백업, 세션 조회 |

## 믿을 수 있는 이유

- **로컬 온리.** 세션 중 LLM 호출, 텔레메트리, 쿠키, 네트워크 없음.
- **크래시 안전.** append-only 원장과 원자적 쓰기: 어디서 끊겨도 재개되고, 정확히 한 번만 착지.
- **되돌릴 수 있음.** 활성화는 소유된 import 한 줄뿐이고, `xout undo`는 xout이 썼다고 증명 가능한 것만 제거.
- **정직함.** 살아남은 행동은 "아직 X를 안 맞았을 뿐"이지 "옳다고 증명된 것"이 아닙니다. 추정 기본값은 추정이라고 표시합니다.

<details>
<summary><strong>이 주장들을 받치는 엔지니어링</strong></summary>

모든 X는 fsync되는 append-only JSONL 이벤트이고, 착지는 콘텐츠 해시를 동반한 원자적 쓰기이며, 세션은 결정적으로 재생되고, 중복 세션은 거부되며, 수동 편집은 착지 전에 감지됩니다. 15번의 X는 6,561개(8축 x 3값)의 가능한 에이전트 공간을 하나로 좁힙니다 - 그리고 살아남은 쪽은 \"아직 X를 안 맞았을 뿐\"이지 \"옳다고 증명된 것\"이 아닙니다. 봉인된 사전등록은 [`docs/prereg/prereg_sealed.json`](docs/prereg/prereg_sealed.json), 동결된 8축 카탈로그는 [`docs/axis_locality_table.md`](docs/axis_locality_table.md)에 있습니다. 8축 카탈로그는 의도적으로 동결되어 있습니다: xout은 로컬 행동 컴파일러이지, 프롬프트 매니저나 클라우드 프로필, 에이전트 오케스트레이터가 아닙니다.

</details>

## Claude Code 플러그인

xout은 Claude Code 안에서 대화로 실행됩니다: `/xout:xout`이 행동 페어를 채팅으로 보여주고, 당신이 X 칠 쪽을 고르면 에이전트는 그 명시적 선택만 기록합니다. `/xout:xout status`, `/xout:xout undo`도 동일합니다.

<details>
<summary><strong>체크섬 검증 플러그인 설치</strong></summary>

[v1.0.0 릴리스](../../releases/tag/v1.0.0)에서 `xout-plugin-1.0.0.zip`, `SHA256SUMS`, `verify_checksums.py`를 받아 한 디렉토리에 두고:

```bash
python3 verify_checksums.py SHA256SUMS \
  --only xout-plugin-1.0.0.zip verify_checksums.py
DEST="$HOME/.local/share/xout-plugin-1.0.0"
test ! -e "$DEST" || { echo "destination already exists: $DEST" >&2; exit 1; }
python3 -m zipfile -e xout-plugin-1.0.0.zip "$DEST"
claude plugin marketplace add "$DEST"
claude plugin install xout@xout-marketplace
```

이후 새 Claude Code 세션에서 `/xout:xout doctor`, `/xout:xout`.

</details>

## 제거

```bash
xout undo        # 비활성화: 소유된 import 한 줄만 제거
```

규칙과 이벤트 히스토리는 `~/.claude/xout/`에 남습니다 (보관도 삭제도 사용자의 것). 패키지를 지워도 이 데이터는 건드리지 않습니다.

## 개발

```bash
python3 -m pip install -e '.[test,release]'
python3 -m pytest tests/ -q
```

CI는 macOS/Linux/Windows에서 Python 3.10-3.14를 커버합니다. 릴리스에는 wheel, sdist, 플러그인 ZIP, `SHA256SUMS`, 아티팩트 출처 증명이 포함됩니다.

MIT © 2026 Brian Kim.
