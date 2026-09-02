#!/usr/bin/env python3
"""README 히어로 SVG를 언어별로 생성한다 - 로컬 텍스트, 폰트 로드 없음, 스크립트 없음.

en은 hero.svg, 나머지는 hero.<lang>.svg. 레이아웃은 같고 문구만 바뀐다.
"""

from __future__ import annotations

import html
from pathlib import Path

COPY = {
    "en": dict(
        title="xout — X out the wrong behavior, keep the rule",
        desc="xout turns one request into an A/B behavior test: the wrong behavior is crossed out with a crimson X and the survivor becomes a local rule.",
        tagline="X OUT THE AI BEHAVIOR YOU NEVER WANT AGAIN", input_label="INPUT", ask="FIX THE BUG.",
        pick_label="X OUT THE WRONG ONE", wrong="SHOULD I START?", kept="FIXED. TESTS PASS.",
        rule_label="GENERATED RULE", rule1="ACT FIRST.", rule2="REPORT AFTER.",
        footer="X IT OUT — KEEP THE RULE.", sub="2 MINUTES → 8 LOCAL RULES",
    ),
    "ko": dict(
        title="xout — 아닌 행동에 X, 남는 규칙",
        desc="xout은 요청 하나를 A/B 행동 테스트로 바꿉니다. 아닌 행동에는 진홍색 X가 그어지고 남은 쪽이 로컬 규칙이 됩니다.",
        tagline="다시 보고 싶지 않은 AI 행동에 X를 치세요", input_label="입력", ask="버그 고쳐줘.",
        pick_label="아닌 쪽에 X", wrong="시작할까요?", kept="고쳤고 테스트 통과.",
        rule_label="만들어진 규칙", rule1="먼저 실행한다.", rule2="그다음 보고한다.",
        footer="X를 치고 — 규칙을 남긴다.", sub="2분 → 로컬 규칙 8줄",
    ),
    "ja": dict(
        title="xout — 違う振る舞いに X、残るのはルール",
        desc="xout はひとつの依頼を A/B の振る舞いテストに変えます。要らない振る舞いには深紅の X がつき、残った側がローカルのルールになります。",
        tagline="二度と要らない AI の振る舞いを X で消す", input_label="入力", ask="バグを直して。",
        pick_label="違う方に X", wrong="始めていい？", kept="直した。テスト通過。",
        rule_label="生成されたルール", rule1="先に実行する。", rule2="あとで報告する。",
        footer="X で消して — ルールを残す。", sub="2 分 → ローカルルール 8 本",
    ),
    "zh": dict(
        title="xout — 给错的行为打 X，留下规则",
        desc="xout 把一次请求变成一道 A/B 行为测试：错的行为被深红色的 X 划掉，留下的那个成为本地规则。",
        tagline="把你再也不想看到的 AI 行为一笔划掉", input_label="输入", ask="修一下这个 bug。",
        pick_label="给错的打 X", wrong="我可以开始吗？", kept="修好了。测试通过。",
        rule_label="生成的规则", rule1="先执行。", rule2="再汇报。",
        footer="划掉它 — 留下规则。", sub="2 分钟 → 8 条本地规则",
    ),
}

SANS = "system-ui, -apple-system, 'Segoe UI', 'Apple SD Gothic Neo', 'Hiragino Sans', 'PingFang SC', sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, 'Apple SD Gothic Neo', 'Hiragino Sans', 'PingFang SC', monospace"


def build(lang: str) -> str:
    c = COPY[lang]
    e = html.escape
    spacing = ' letter-spacing="1"' if lang == "en" else ""
    tag_spacing = ' letter-spacing="2"' if lang == "en" else ""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="420" viewBox="0 0 1200 420" role="img" aria-labelledby="title desc">
  <title id="title">{e(c["title"])}</title>
  <desc id="desc">{e(c["desc"])}</desc>
  <rect width="1200" height="420" fill="#F7F3EA"/>
  <text x="54" y="58" fill="#171717" font-family="{SANS}" font-size="34" font-weight="800" letter-spacing="5">XOUT</text>
  <text x="54" y="88" fill="#6E6A63" font-family="{SANS}" font-size="15" font-weight="700"{tag_spacing}>{e(c["tagline"])}</text>
  <text x="54" y="132" fill="#6E6A63" font-family="{MONO}" font-size="18" font-weight="700"{spacing}>{e(c["input_label"])}</text>
  <rect x="54" y="148" width="250" height="62" rx="9" fill="#D9D4C9" stroke="#171717" stroke-width="4"/>
  <text x="74" y="188" fill="#171717" font-family="{MONO}" font-size="24" font-weight="800">{e(c["ask"])}</text>
  <text x="308" y="187" fill="#6E6A63" font-family="{SANS}" font-size="28" font-weight="800">→</text>
  <text x="334" y="132" fill="#6E6A63" font-family="{MONO}" font-size="18" font-weight="700"{spacing}>{e(c["pick_label"])}</text>
  <rect x="334" y="148" width="286" height="62" rx="9" fill="#F7F3EA" stroke="#171717" stroke-width="4"/>
  <text x="353" y="188" fill="#171717" font-family="{MONO}" font-size="20" font-weight="800">{e(c["wrong"])}</text>
  <g stroke="#D92332" stroke-width="10" stroke-linecap="round">
    <path d="M344 156L610 202"/>
    <path d="M610 156L344 202"/>
  </g>
  <rect x="334" y="228" width="286" height="62" rx="9" fill="#D9D4C9" stroke="#171717" stroke-width="4"/>
  <text x="353" y="268" fill="#171717" font-family="{MONO}" font-size="20" font-weight="800">{e(c["kept"])}</text>
  <text x="633" y="224" fill="#6E6A63" font-family="{SANS}" font-size="28" font-weight="800">→</text>
  <text x="670" y="132" fill="#6E6A63" font-family="{MONO}" font-size="18" font-weight="700"{spacing}>{e(c["rule_label"])}</text>
  <rect x="670" y="148" width="476" height="142" rx="12" fill="#F7F3EA" stroke="#171717" stroke-width="5"/>
  <path d="M694 178H1122" stroke="#171717" stroke-width="5" stroke-linecap="round"/>
  <text x="694" y="226" fill="#171717" font-family="{MONO}" font-size="24" font-weight="800">{e(c["rule1"])}</text>
  <text x="694" y="262" fill="#171717" font-family="{MONO}" font-size="24" font-weight="800">{e(c["rule2"])}</text>
  <text x="54" y="356" fill="#171717" font-family="{SANS}" font-size="24" font-weight="800"{spacing}>{e(c["footer"])}</text>
  <text x="54" y="386" fill="#6E6A63" font-family="{MONO}" font-size="16" font-weight="700"{spacing}>{e(c["sub"])}</text>
</svg>
'''


def main() -> None:
    out = Path(__file__).resolve().parents[1] / ".github" / "assets"
    for lang in COPY:
        name = "hero.svg" if lang == "en" else f"hero.{lang}.svg"
        (out / name).write_text(build(lang), encoding="utf-8")
        print("wrote", name)


if __name__ == "__main__":
    main()
