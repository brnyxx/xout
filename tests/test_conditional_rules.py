"""맥락 조건부 컴파일 - 두 맥락의 긋기 증거가 갈리면 조건부 규칙이 나온다.

routine 장면과 irreversible 장면에서 같은 축의 생존값이 다르게 측정되면,
평시 문장 + "되돌리기 어려운 작업에서는 ..." 절이 합성된다. 두 맥락이
같은 값으로 수렴하면 무조건부 문장 그대로다. 한 맥락만 측정되면 분기하지
않는다 - 조건은 추정이 아니라 증거에서만 나온다.
"""

from __future__ import annotations

from xout.compiler import (
    IRREVERSIBLE_CONDITION_PREFIX,
    RULE_TEXT,
    compile_rules,
    conditional_rule_text,
)
from xout.events import Refutation, strike


def _strike(scene_id: str, axis: str, value: str, suffix: str) -> object:
    return strike(
        session_id="cond",
        pair_id=f"{scene_id}:{axis}:{value}|x-{suffix}",
        axis=axis,
        scene_id=scene_id,
        target="left",
        refutations=(
            Refutation(
                axis=axis,
                value=value,
                fragment_id=f"frag-{suffix}",
                side="left",
            ),
        ),
    )


def _rule(events, axis: str):
    return {rule.axis: rule for rule in compile_rules(events)}[axis]


def test_diverging_contexts_compile_a_conditional_rule() -> None:
    # routine에서 ask_first와 propose_then_act에 X -> routine 생존 {act}.
    # irreversible에서 act_then_report에 X -> 생존 {ask, propose} -> ask 선두.
    events = [
        _strike("scn-bugfix", "autonomy", "ask_first", "r1"),
        _strike("scn-bugfix", "autonomy", "propose_then_act", "r2"),
        _strike("scn-risky", "autonomy", "act_then_report", "i1"),
    ]
    rule = _rule(events, "autonomy")
    routine_text = RULE_TEXT[("autonomy", "act_then_report")]
    assert rule.value == "act_then_report"
    assert rule.text.startswith(routine_text)
    assert IRREVERSIBLE_CONDITION_PREFIX in rule.text
    assert rule.text == conditional_rule_text(
        "autonomy", "act_then_report", "ask_first"
    )


def test_agreeing_contexts_stay_unconditional() -> None:
    events = [
        _strike("scn-bugfix", "autonomy", "ask_first", "r1"),
        _strike("scn-risky", "autonomy", "ask_first", "i1"),
    ]
    rule = _rule(events, "autonomy")
    assert IRREVERSIBLE_CONDITION_PREFIX not in rule.text
    assert rule.text == RULE_TEXT[("autonomy", rule.value)]


def test_single_context_evidence_never_forks() -> None:
    events = [_strike("scn-bugfix", "autonomy", "ask_first", "r1")]
    rule = _rule(events, "autonomy")
    assert IRREVERSIBLE_CONDITION_PREFIX not in rule.text
    assert rule.text == RULE_TEXT[("autonomy", rule.value)]


def test_legacy_scene_counts_as_routine() -> None:
    events = [
        _strike("scn-pagination-fix", "autonomy", "ask_first", "r1"),
        _strike("scn-risky", "autonomy", "act_then_report", "i1"),
    ]
    rule = _rule(events, "autonomy")
    assert IRREVERSIBLE_CONDITION_PREFIX in rule.text


def test_conditional_text_is_epistemically_clean() -> None:
    from xout.compiler import EPISTEMIC_TOKENS, IRREVERSIBLE_CLAUSE

    for (axis, routine_value), _ in IRREVERSIBLE_CLAUSE.items():
        for (axis2, irreversible_value), _ in IRREVERSIBLE_CLAUSE.items():
            if axis != axis2 or routine_value == irreversible_value:
                continue
            text = conditional_rule_text(axis, routine_value, irreversible_value)
            lowered = text.lower()
            for token in EPISTEMIC_TOKENS:
                assert token not in lowered, (axis, token)
