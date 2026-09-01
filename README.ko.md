<div align="center">

<h1>xout</h1>

<img src=".github/assets/logo.svg" alt="xout 로고: 굵은 크림슨 X로 지워진 행동 카드와 그 아래 남은 규칙 한 줄" width="96">

**다시 보고 싶지 않은 AI 행동에 X를 치세요.**

<img src=".github/assets/hero.svg" alt="버그 고쳐줘가 A/B 행동 테스트가 되는 그림: 시작할까요?에 X가 그어지고, 고쳤고 테스트 통과가 살아남아 먼저 실행하고 나중에 보고한다는 규칙이 된다" width="920">

[English](README.md) · [소개 사이트](https://brnyxx.github.io/popper/)

</div>

코딩 에이전트가 행동할 수 있는 두 가지 방식을 보여줍니다. 아닌 쪽에 X를 치세요. 2분, 15번의 X 후에 살아남은 선택들이 Claude Code가 `CLAUDE.md`에서 읽는 로컬 규칙 8줄로 컴파일됩니다.

```bash
uvx xout
```

이게 전부입니다. 브라우저가 열리고, 2분 정도 X를 치면, 에이전트에게 규칙 8줄이 생깁니다.

**클라우드 없음. 텔레메트리 없음. LLM 호출 없음. 롤백은 한 줄.**

> **상태:** xout 리네임(v2.0)은 진행 중입니다. v1.3.1까지의 릴리스는 이전 이름 `popper`를 사용합니다. v2.0의 PyPI 배포 전까지는 아래 접힌 섹션의 설치 경로를 사용하세요.

<details>
<summary><strong>지금 v1.3.1로 실행하기</strong> (릴리스 wheel, 이전 이름)</summary>

macOS 또는 Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install \
  https://github.com/brnyxx/popper/releases/download/v1.3.1/popper-1.3.1-py3-none-any.whl
.venv/bin/popper doctor
.venv/bin/popper open
```

Windows PowerShell:

```powershell
py -3 -m venv .venv
.venv\Scripts\python -m pip install `
  https://github.com/brnyxx/popper/releases/download/v1.3.1/popper-1.3.1-py3-none-any.whl
.venv\Scripts\popper doctor
.venv\Scripts\popper open
```

</details>

## 동작 방식

1. **싫은 행동에 X를 칩니다.** 매 페어는 실제 에이전트 행동 둘을 비교합니다: 먼저 물어보기 vs 먼저 실행하기, 요청 범위 엄수 vs 적극적 정리, 테스트 먼저 vs 테스트 나중 같은 것들.
2. **xout이 컴파일합니다.** 살아남은 선택들이 실행 가능한 규칙 8줄이 되어 근거와 출처를 담은 채 `~/.claude/xout/`에 원자적으로 기록됩니다.
3. **클릭 한 번으로 적용합니다.** 완료 화면에서 "지금 적용할까요?"를 물으며, 예라고 하면 `~/.claude/CLAUDE.md`에 xout 소유의 `@import` 한 줄만 추가됩니다. `xout undo`가 그 한 줄만 제거합니다.

새 Claude Code 세션에서 예전에 거슬리던 요청을 다시 해보고 규칙이 지켜지는지 확인하세요. 규칙이 낡았다 싶으면 다시 `xout`.

<details>
<summary><strong>실제 15회 X 브라우저 세션 보기</strong> (1.6 MB GIF)</summary>
<br>
<img src=".github/assets/demo.gif" alt="실제 브라우저 UI가 0에서 15회 긋기까지 진행되어 로컬 규칙을 컴파일하고 세션을 완료하는 모습" width="860">
</details>

## 얻는 것

15번째 X 이후 `~/.claude/xout/`에 세 파일이 착지합니다:

| 파일 | 내용 |
|---|---|
| `XOUT.md` | Claude Code용 실행 가능한 규칙 8줄 |
| `manifest.json` | 규칙 값, 확신 라벨, 출처, 콘텐츠 해시 |
| `settings.xout.json` | 검토 가능한 설정 제안 |

X로 직접 확정한 규칙은 **확정**, xout이 묻지 않고 추정한 기본값은 정직하게 **추정**으로 표시되고 다시 고르기 대기열에 들어갑니다. 명시적 동의 없이 활성화되는 것은 없습니다.

*(v1.3.1에서는 같은 세 파일이 `POPPER.md`, `manifest.json`, `settings.popper.json`으로 `~/.claude/popper/`에 착지합니다. v2.0이 경로를 리네임하고 마이그레이션합니다.)*

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

append-only JSONL 이벤트, fsync, 프로세스 락, 봉인된 픽스처/세션 다이제스트, 결정적 재생, 원자적 교체, 루프백 Host/Origin 검사, 중복 세션 거부, 착지 전 수동 수정 감지. 봉인된 사전등록은 [`docs/prereg/prereg_sealed.json`](docs/prereg/prereg_sealed.json), 동결된 8축 카탈로그는 [`docs/axis_locality_table.md`](docs/axis_locality_table.md)에 있습니다. 8축 카탈로그는 의도적으로 동결되어 있습니다: xout은 로컬 행동 컴파일러이지, 프롬프트 매니저나 클라우드 프로필, 에이전트 오케스트레이터가 아닙니다.

</details>

## Claude Code 플러그인

xout은 Claude Code 안에서 `/xout`으로도 실행됩니다 (v1.3.1: `/popper:popper ...`).

<details>
<summary><strong>체크섬 검증 플러그인 설치 (v1.3.1)</strong></summary>

[v1.3.1 릴리스](../../releases/tag/v1.3.1)에서 `popper-plugin-1.3.1.zip`, `SHA256SUMS`, `verify_checksums.py`를 받아 한 디렉토리에 두고:

```bash
python3 verify_checksums.py SHA256SUMS \
  --only popper-plugin-1.3.1.zip verify_checksums.py
DEST="$HOME/.local/share/popper-plugin-1.3.1"
test ! -e "$DEST" || { echo "destination already exists: $DEST" >&2; exit 1; }
python3 -m zipfile -e popper-plugin-1.3.1.zip "$DEST"
claude plugin marketplace add "$DEST"
claude plugin install popper@popper-marketplace
```

이후 새 Claude Code 세션에서 `/popper:popper doctor`, `/popper:popper open`.

</details>

## 제거

```bash
xout undo        # 비활성화: 소유된 import 한 줄만 제거
```

규칙과 이벤트 히스토리는 `~/.claude/xout/`에 남습니다 (보관도 삭제도 사용자의 것). 패키지를 지워도 이 데이터는 건드리지 않습니다.

## 개발

```bash
python3 -m pip install -e '.[test,e2e,release]'
python3 -m pytest tests/ -q
```

CI는 Python 3.10-3.14, macOS/Linux/Windows, Chromium/Firefox/WebKit을 커버합니다. 릴리스에는 wheel, sdist, 플러그인 ZIP, `SHA256SUMS`, 아티팩트 출처 증명이 포함됩니다.

MIT © 2026 Brian Kim.
