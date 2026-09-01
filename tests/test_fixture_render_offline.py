"""AC7 - 세션 전체가 고정 픽스처 + 레포 읽기전용 슬롯 치환만으로 렌더되는지 검증한다.

v2: 3장면(routine 2 + irreversible 1) 팩. 런타임 LLM/외부 네트워크 호출 0회,
slot span 맵을 통한 strike -> 축 귀속, 장면 교차 라운드 순서를 확인한다.
"""

from __future__ import annotations

import ast
from itertools import combinations
from pathlib import Path

import pytest

from xout import fixtures as fx
from xout.counter import DEFAULT_CATALOG
from xout.events import Refutation


def _pack() -> fx.FixturePack:
    return fx.load_pack()


def _make_python_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "api.py").write_text("def api():\n    return 1\n", encoding="utf-8")
    (repo / "src" / "util.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "manage.py").write_text("", encoding="utf-8")
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "vendor.js").write_text("x", encoding="utf-8")
    return repo


class TestScenePack:
    """(1) 3장면이 8축 전부를 덮고, 맥락 클래스가 동결 맵과 일치한다."""

    def test_pack_has_three_scenes_with_frozen_contexts(self) -> None:
        pack = _pack()
        assert pack.scene_ids == ("scn-bugfix", "scn-feature", "scn-risky")
        for scene in pack.scenes:
            assert scene.context == fx.SCENE_CONTEXTS[scene.scene_id]
        contexts = {scene.context for scene in pack.scenes}
        assert contexts == set(fx.SCENE_CONTEXT_VALUES)

    def test_scenes_cover_the_whole_catalog(self) -> None:
        pack = _pack()
        covered: set[str] = set()
        for scene in pack.scenes:
            assert len(scene.slot_axes) == 5
            covered.update(scene.slot_axes)
        assert covered == set(DEFAULT_CATALOG)

    def test_cross_context_axes_appear_in_both_contexts(self) -> None:
        pack = _pack()
        by_context: dict[str, set[str]] = {}
        for scene in pack.scenes:
            by_context.setdefault(scene.context, set()).update(scene.slot_axes)
        cross = by_context[fx.CONTEXT_ROUTINE] & by_context[fx.CONTEXT_IRREVERSIBLE]
        assert cross == {
            "autonomy",
            "error_behavior",
            "verification",
            "dependency_policy",
            "commit_style",
        }

    def test_every_scene_axis_renders_all_three_value_pairs(self) -> None:
        pack = _pack()
        for scene in pack.scenes:
            for axis in scene.slot_axes:
                values = DEFAULT_CATALOG[axis]
                assert len(values) == 3
                for left_value, right_value in combinations(values, 2):
                    pair = fx.render_pair(
                        pack, scene.scene_id, axis, left_value, right_value,
                        fx.GENERIC_SKIN,
                    )
                    assert pair.axis == axis
                    assert pair.scene_id == scene.scene_id
                    assert pair.left.text and pair.right.text
                    assert fx.contrast_span(pair.left).value == left_value
                    assert fx.contrast_span(pair.right).value == right_value

    def test_render_all_pairs_covers_all_scene_axis_combos(self) -> None:
        pairs = fx.render_all_pairs(_pack(), fx.GENERIC_SKIN)
        assert len(pairs) == 45  # 3장면 x 5축 x 3조합
        assert {p.axis for p in pairs} == set(DEFAULT_CATALOG)

    def test_first_round_interleaves_scenes_before_repeating(self) -> None:
        pairs = fx.render_all_pairs(_pack(), fx.GENERIC_SKIN)
        first_round = pairs[:15]
        assert [p.scene_id for p in first_round[:5]] == ["scn-bugfix"] * 5
        assert [p.scene_id for p in first_round[5:10]] == ["scn-feature"] * 5
        assert [p.scene_id for p in first_round[10:15]] == ["scn-risky"] * 5
        # 콜드 오픈: 첫 페어는 자율성 축이다.
        assert first_round[0].axis == "autonomy"
        # 한 라운드 안에서 (장면, 축)은 중복되지 않는다.
        seen = [(p.scene_id, p.axis) for p in first_round]
        assert len(set(seen)) == 15

    def test_render_rejects_axis_outside_the_scene(self) -> None:
        with pytest.raises(fx.FixtureViolation):
            fx.render_pair(
                _pack(), "scn-bugfix", "verification", "always_run", "on_risky",
                fx.GENERIC_SKIN,
            )


class TestRenderDeterminism:
    """(2) 같은 입력이면 같은 출력이다 - 타임스탬프/난수 오염 없음."""

    def test_same_inputs_render_identical_pairs(self, tmp_path: Path) -> None:
        repo = _make_python_repo(tmp_path)
        skin_a = fx.scan_repo_skin(repo)
        skin_b = fx.scan_repo_skin(repo)
        assert skin_a == skin_b

        first = fx.render_pair(
            fx.load_pack(), "scn-bugfix", "autonomy", "ask_first", "act_then_report",
            skin_a,
        )
        second = fx.render_pair(
            fx.load_pack(), "scn-bugfix", "autonomy", "ask_first", "act_then_report",
            skin_b,
        )
        assert first == second
        assert first.pair_id == "scn-bugfix:autonomy:ask_first|act_then_report"

    def test_risky_scene_render_is_deterministic(self) -> None:
        pack = _pack()
        first = fx.render_pair(
            pack, "scn-risky", "verification", "always_run", "trust_static",
            fx.GENERIC_SKIN,
        )
        second = fx.render_pair(
            pack, "scn-risky", "verification", "always_run", "trust_static",
            fx.GENERIC_SKIN,
        )
        assert first == second


class TestSpanReverseAttribution:
    """(3) slot span 맵으로 임의 span -> 축 역귀속이 보장된다."""

    def test_any_span_inside_a_slot_attributes_to_its_axis(self) -> None:
        pair = fx.render_pair(
            _pack(), "scn-feature", "commit_style", "conventional", "narrative",
            fx.GENERIC_SKIN,
        )
        for transcript in (pair.left, pair.right):
            for span in transcript.spans:
                if span.axis is None:
                    continue
                middle = (span.start + span.end) // 2
                assert fx.attribute_span(transcript, span.start, span.end) == span
                refutation = fx.refutation_for_span(transcript, middle, middle + 1)
                assert isinstance(refutation, Refutation)
                assert refutation.axis == span.axis
                assert refutation.value == span.value
                assert refutation.fragment_id == span.fragment_id
                assert refutation.side == transcript.side

    def test_fragment_id_attributes_through_span_map(self) -> None:
        pair = fx.render_pair(
            _pack(), "scn-bugfix", "test_discipline", "test_first", "on_request",
            fx.GENERIC_SKIN,
        )
        span = fx.contrast_span(pair.left)
        refutation = fx.refutation_for_fragment(pair.left, span.fragment_id)
        assert refutation.axis == "test_discipline"
        assert refutation.value == "test_first"

    def test_static_fragment_never_attributes_to_an_axis(self) -> None:
        pair = fx.render_pair(
            _pack(), "scn-bugfix", "autonomy", "ask_first", "propose_then_act",
            fx.GENERIC_SKIN,
        )
        static = next(s for s in pair.left.spans if s.role == fx.ROLE_STATIC)
        with pytest.raises(fx.FixtureViolation):
            fx.refutation_for_span(pair.left, static.start, static.end)

    def test_cross_slot_span_is_rejected(self) -> None:
        pair = fx.render_pair(
            _pack(), "scn-bugfix", "autonomy", "ask_first", "propose_then_act",
            fx.GENERIC_SKIN,
        )
        first, second = pair.left.spans[0], pair.left.spans[1]
        with pytest.raises(fx.FixtureViolation):
            fx.attribute_span(pair.left, first.end - 1, second.start + 1)

    def test_separator_gap_offset_is_rejected(self) -> None:
        pair = fx.render_pair(
            _pack(), "scn-bugfix", "autonomy", "ask_first", "propose_then_act",
            fx.GENERIC_SKIN,
        )
        gap_offset = pair.left.spans[0].end  # 조각 사이 구분자 시작 위치
        with pytest.raises(fx.FixtureViolation):
            fx.span_at(pair.left, gap_offset)


class TestPairContrastConfinement:
    """(4) 좌우 페어는 대비 축 슬롯만 다르고 나머지는 동일하다."""

    def test_pair_differs_only_inside_contrast_slot(self) -> None:
        pack = _pack()
        for scene in pack.scenes:
            for axis in scene.slot_axes:
                values = DEFAULT_CATALOG[axis]
                pair = fx.render_pair(
                    pack, scene.scene_id, axis, values[0], values[2], fx.GENERIC_SKIN
                )
                left_span = fx.contrast_span(pair.left)
                right_span = fx.contrast_span(pair.right)

                assert (
                    pair.left.text[: left_span.start]
                    == pair.right.text[: right_span.start]
                )
                assert (
                    pair.left.text[left_span.end :]
                    == pair.right.text[right_span.end :]
                )
                assert (
                    pair.left.text[left_span.start : left_span.end]
                    != pair.right.text[right_span.start : right_span.end]
                )

    def test_background_slots_share_mined_mode_values_on_both_sides(self) -> None:
        pair = fx.render_pair(
            _pack(), "scn-bugfix", "error_behavior", "stop_and_report", "self_heal",
            fx.GENERIC_SKIN,
        )
        for left_span, right_span in zip(pair.left.spans, pair.right.spans):
            if left_span.role != fx.ROLE_BACKGROUND:
                continue
            assert right_span.role == fx.ROLE_BACKGROUND
            assert left_span.axis == right_span.axis
            assert left_span.value == right_span.value
            # 배경 슬롯은 채굴 최빈값(카탈로그 index 0)으로 고정된다.
            assert left_span.value == DEFAULT_CATALOG[left_span.axis][0]


class TestRepoSkinSubstitution:
    """(5) 레포 읽기전용 스캔으로 스킨 치환하고, 빈/비코드 레포는 폴백한다."""

    def test_python_repo_skin_is_scanned_from_names_only(self, tmp_path: Path) -> None:
        repo = _make_python_repo(tmp_path)
        skin = fx.scan_repo_skin(repo)
        assert skin.lang == "Python"
        assert skin.framework == "Django"  # manage.py 마커
        assert skin.file == "manage.py"  # 지배 확장자 중 최상위 경로
        assert skin.generic is False

    def test_rendered_text_substitutes_all_skin_placeholders(
        self, tmp_path: Path
    ) -> None:
        repo = _make_python_repo(tmp_path)
        skin = fx.scan_repo_skin(repo)
        pair = fx.render_pair(
            fx.load_pack(), "scn-feature", "commit_style", "conventional",
            "no_auto_commit", skin,
        )
        for transcript in (pair.left, pair.right):
            assert "manage.py" in transcript.text
            assert "Python" in transcript.text
            for placeholder in fx.SKIN_PLACEHOLDERS:
                assert placeholder not in transcript.text

    def test_empty_repo_falls_back_to_generic_skin(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        assert fx.scan_repo_skin(empty) == fx.GENERIC_SKIN

    def test_non_code_repo_falls_back_to_generic_skin(self, tmp_path: Path) -> None:
        noncode = tmp_path / "noncode"
        noncode.mkdir()
        (noncode / "notes.txt").write_text("메모", encoding="utf-8")
        (noncode / "data.csv").write_text("a,b\n", encoding="utf-8")
        assert fx.scan_repo_skin(noncode) == fx.GENERIC_SKIN

    def test_missing_repo_root_falls_back_to_generic_skin(self, tmp_path: Path) -> None:
        assert fx.scan_repo_skin(tmp_path / "no-such-dir") == fx.GENERIC_SKIN

    def test_generic_skin_still_renders_full_session(self) -> None:
        pairs = fx.render_all_pairs(_pack(), fx.GENERIC_SKIN)
        for pair in pairs:
            assert "README.md" in pair.left.text
            for placeholder in fx.SKIN_PLACEHOLDERS:
                assert placeholder not in pair.left.text


class TestOfflineGuarantee:
    """(6) fixtures.py 소스에 네트워크/서브프로세스 import가 없다 - 정적 검사."""

    ALLOWED_ROOTS = frozenset(
        {
            "__future__",
            "dataclasses",
            "itertools",
            "json",
            "logging",
            "pathlib",
            "typing",
            "collections",
            "xout",
        }
    )

    def _import_roots(self) -> set[str]:
        source = (Path(fx.__file__)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        return roots

    def test_no_network_or_subprocess_imports(self) -> None:
        roots = self._import_roots()
        for banned in ("socket", "http", "urllib", "subprocess", "asyncio"):
            assert banned not in roots

    def test_only_stdlib_and_popper_imports(self) -> None:
        roots = self._import_roots()
        assert roots <= self.ALLOWED_ROOTS, sorted(roots - self.ALLOWED_ROOTS)
