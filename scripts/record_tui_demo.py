#!/usr/bin/env python3
"""실제 xout 터미널 세션을 구동해 데모 GIF로 렌더한다.

릴리스 메인테이너 전용 에셋 도구다: Pillow와 한국어 시스템 폰트가 필요하고,
런타임은 절대 이 모듈을 import하지 않는다. 프레임에 그려지는 페어와 규칙은
전부 실제 ColdOpenSession이 방금 생성한 것이다.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from xout.state import ColdOpenSession  # noqa: E402
from xout.store import EventStore  # noqa: E402

# 화면 크롬 문자열 - 페어/규칙 본문은 실제 세션이 언어별 팩에서 만든다.
CHROME = {
    "ko": {
        "cmd": "$ uvx xout",
        "tagline": "xout - 아닌 쪽에 X를 치세요.",
        "promise": "2분 뒤, 에이전트에게 규칙 8줄이 생깁니다.",
        "strike_hint": "xout - 아닌 쪽에 X를 치세요.",
        "complete": "세션 완료 - 컴파일된 규칙:",
        "apply": "지금 CLAUDE.md에 적용할까요? [y/N] ",
        "applied": "적용 완료 - @import 한 줄이 추가됐다. 취소는 xout undo",
        "targets": "다른 도구에도: xout enable --grant --target codex|opencode|gemini|copilot|pi|omp|gjc|kiro|agents",
    },
    "en": {
        "cmd": "$ uvx xout --lang en",
        "tagline": "xout - cross out the one you never want.",
        "promise": "Two minutes from now, your agent has eight rules.",
        "strike_hint": "xout - cross out the one you never want.",
        "complete": "Session complete - compiled rules:",
        "apply": "Apply to CLAUDE.md now? [y/N] ",
        "applied": "Applied - one @import line added. Undo with xout undo",
        "targets": "Other tools: xout enable --grant --target codex|opencode|gemini|copilot|pi|omp|gjc|kiro|agents",
    },
    "ja": {
        "cmd": "$ uvx xout --lang ja",
        "tagline": "xout - 二度と見たくない方に X を。",
        "promise": "2分後、あなたのエージェントにルールが8行できます。",
        "strike_hint": "xout - 二度と見たくない方に X を。",
        "complete": "セッション完了 - コンパイルされたルール:",
        "apply": "今すぐ CLAUDE.md に適用しますか？ [y/N] ",
        "applied": "適用完了 - @import を1行追加。取り消しは xout undo",
        "targets": "他のツールにも: xout enable --grant --target codex|opencode|gemini|copilot|pi|omp|gjc|kiro|agents",
    },
    "zh": {
        "cmd": "$ uvx xout --lang zh",
        "tagline": "xout - 给你再也不想看到的那个打 X。",
        "promise": "两分钟后，你的智能体就有了 8 条规则。",
        "strike_hint": "xout - 给你再也不想看到的那个打 X。",
        "complete": "会话完成 - 编译出的规则:",
        "apply": "现在应用到 CLAUDE.md 吗？ [y/N] ",
        "applied": "已应用 - 添加了一行 @import。撤销: xout undo",
        "targets": "也接入其他工具: xout enable --grant --target codex|opencode|gemini|copilot|pi|omp|gjc|kiro|agents",
    },
}

#: 언어별 시스템 폰트 - CJK 글리프 커버리지가 언어마다 다르다.
FONT_BY_LANG = {
    "ko": "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "en": "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "ja": "/System/Library/Fonts/ヒラギノ角ゴシック W4.ttc",
    "zh": "/System/Library/Fonts/Hiragino Sans GB.ttc",
}

WIDTH, HEIGHT = 960, 608
BG = "#141412"
INK = "#F7F3EA"
MUTED = "#8A867D"
CRIMSON = "#E4374F"
GREEN = "#7FB069"
MARGIN_X, MARGIN_TOP = 34, 52
LINE_HEIGHT = 25
FONT_KO = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
FONT_SIZE = 17

# 데모가 마지막에 보여줄 규칙 세트가 일관되도록, 각 축에서 살릴 값.
PREFERRED_VALUES = {
    "autonomy": "act_then_report",
    "scope_adherence": "adjacent_fix_ok",
    "test_discipline": "test_after",
    "comment_doc": "minimal",
    "error_behavior": "retry_then_report",
    "commit_style": "no_auto_commit",
    "verification": "always_run",
    "dependency_policy": "prefer_existing",
}

# 고위험 장면에서는 다른 값을 살린다 - 조건부 규칙이 컴파일되는 시연.
RISKY_PREFERRED_VALUES = {**PREFERRED_VALUES, "autonomy": "ask_first"}


def _font(lang: str = "ko") -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BY_LANG.get(lang, FONT_KO), FONT_SIZE)


def _wrap(font: ImageFont.FreeTypeFont, text: str, max_width: int) -> list[str]:
    lines: list[str] = []
    for raw in text.split("\n"):
        current = ""
        for char in raw:
            if font.getlength(current + char) > max_width:
                lines.append(current)
                current = char
            else:
                current += char
        lines.append(current)
    return lines


class Frame:
    """터미널 한 화면 - (텍스트, 색, 취소선 여부) 줄의 목록."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, bool]] = []

    def line(self, text: str = "", color: str = INK, strike: bool = False) -> None:
        self.rows.append((text, color, strike))

    def render(self, font: ImageFont.FreeTypeFont) -> Image.Image:
        image = Image.new("RGB", (WIDTH, HEIGHT), BG)
        draw = ImageDraw.Draw(image)
        for offset, color in ((0, "#3A3A36"), (22, "#3A3A36"), (44, "#3A3A36")):
            draw.ellipse((18 + offset, 16, 30 + offset, 28), fill=color)
        draw.text((WIDTH // 2 - 20, 14), "xout", font=font, fill=MUTED)
        y = MARGIN_TOP
        for text, color, strike in self.rows:
            for piece in _wrap(font, text, WIDTH - MARGIN_X * 2):
                if y > HEIGHT - LINE_HEIGHT:
                    break
                draw.text((MARGIN_X, y), piece, font=font, fill=color)
                if strike and piece.strip():
                    length = font.getlength(piece)
                    draw.line(
                        (MARGIN_X, y + FONT_SIZE // 2 + 2,
                         MARGIN_X + length, y + FONT_SIZE // 2 + 2),
                        fill=CRIMSON,
                        width=2,
                    )
                y += LINE_HEIGHT
        return image


def _divergent(left: str, right: str) -> tuple[str, str]:
    """공통 서두(사용자 요청)를 걷어내고 실제로 갈라지는 줄부터 보여준다."""
    left_lines = [line for line in left.split("\n") if line.strip()]
    right_lines = [line for line in right.split("\n") if line.strip()]
    index = 0
    while (
        index < min(len(left_lines), len(right_lines)) - 1
        and left_lines[index] == right_lines[index]
    ):
        index += 1
    return left_lines[index], right_lines[index]


def _pair_frame(snap, typed: str | None, struck: str | None, chrome: dict) -> Frame:
    frame = Frame()
    frame.line(chrome["cmd"], GREEN)
    frame.line(chrome["strike_hint"], MUTED)
    frame.line()
    pair = snap.pair
    frame.line(f"[{snap.slots_used + 1}/{snap.slots_total}] {pair.axis_label}", INK)
    frame.line()
    left_text, right_text = _divergent(pair.left_text, pair.right_text)
    frame.line("  (1) " + left_text,
               CRIMSON if struck == "left" else INK, strike=struck == "left")
    frame.line()
    frame.line("  (2) " + right_text,
               CRIMSON if struck == "right" else INK, strike=struck == "right")
    frame.line()
    frame.line("X> " + (typed or ""), GREEN)
    return frame


def _completion_frames(snap, applied: bool, chrome: dict) -> list[Frame]:
    base = Frame()
    base.line(chrome["cmd"], GREEN)
    base.line()
    base.line(chrome["complete"], INK)
    for rule in snap.rules[:8]:
        base.line("  - " + rule.text, MUTED)
    frames = [base]
    ask = Frame()
    ask.rows = list(base.rows)
    ask.line()
    ask.line(chrome["apply"] + ("y" if applied else ""), GREEN)
    frames.append(ask)
    done = Frame()
    done.rows = list(ask.rows)
    done.line()
    done.line(chrome["applied"], INK)
    done.line(chrome["targets"], MUTED)
    frames.append(done)
    return frames


def capture(output: Path, lang: str = "ko") -> None:
    chrome = CHROME[lang]
    font = _font(lang)
    frames: list[Image.Image] = []
    durations: list[int] = []

    def emit(frame: Frame, centiseconds: int) -> None:
        frames.append(frame.render(font))
        durations.append(centiseconds * 10)

    with tempfile.TemporaryDirectory() as tmp:
        store = EventStore(Path(tmp))
        session = ColdOpenSession(store=store, land_dir=Path(tmp), lang=lang)
        intro = Frame()
        intro.line(chrome["cmd"], GREEN)
        intro.line()
        intro.line(chrome["tagline"], INK)
        intro.line(chrome["promise"], MUTED)
        emit(intro, 170)
        slot = 0
        while True:
            snap = session.snapshot()
            if snap.session_complete or snap.pair is None:
                break
            table = (
                RISKY_PREFERRED_VALUES
                if snap.pair.scene_id == "scn-risky"
                else PREFERRED_VALUES
            )
            preferred = table.get(snap.pair.axis)
            if snap.pair.left_value == preferred:
                target = "right"
            elif snap.pair.right_value == preferred:
                target = "left"
            else:
                target = "left" if slot % 2 == 0 else "right"
            choice = "1" if target == "left" else "2"
            show, typed, struck = (110, 45, 70) if slot < 3 else (38, 14, 26)
            emit(_pair_frame(snap, None, None, chrome), show)
            emit(_pair_frame(snap, choice, None, chrome), typed)
            emit(_pair_frame(snap, choice, target, chrome), struck)
            session.strike(target, expected_pair_id=snap.pair.pair_id)
            slot += 1
        final = session.snapshot()
        for index, frame in enumerate(
            _completion_frames(final, applied=True, chrome=chrome)
        ):
            emit(frame, (170, 120, 240)[index])

    output.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        output,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    print(f"wrote {output} ({len(frames)} frames)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--lang", choices=tuple(CHROME), default="ko")
    args = parser.parse_args()
    capture(args.output, lang=args.lang)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
