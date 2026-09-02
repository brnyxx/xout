"""AC4 - 세션 이벤트를 8축 실행 룰(XOUT.md)과 인식론 사이드카(manifest.json)로 컴파일한다.

컴파일러는 순수 fold 파생만 사용한다. 카운터/등급/판정은 저장하지 않고 매 호출마다
이벤트 스트림에서 다시 계산한다. 산출 경로는 ~/.claude/popper/ 단독이며 사용자 파일에는
쓰지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from xout.atomic import atomic_write_text
from xout.counter import DEFAULT_CATALOG, AxisCatalog, fold
from xout.events import EventType, StrikeEvent
from xout.fixtures import CONTEXT_IRREVERSIBLE, CONTEXT_ROUTINE, SCENE_CONTEXTS
from xout.locking import base_lock

logger = logging.getLogger(__name__)

CATALOG_VERSION = "v2"
METRIC_SPEC_VERSION = "v1"
MANIFEST_VERSION = "1"
SCOPE = "global"

XOUT_MD = "XOUT.md"
MANIFEST_JSON = "manifest.json"
SETTINGS_JSON = "settings.xout.json"
OUTPUT_FILES = (XOUT_MD, MANIFEST_JSON, SETTINGS_JSON)

DEFAULT_PREREG_REF = "docs/prereg/prereg_sealed.json"

GRADE_DISCRIMINATED = "discriminated"
GRADE_INDISCRIMINATE = "indiscriminate"
GRADE_UNTESTED = "untested"
GRADE_UNSTABLE = "unstable"

GRADE_LABELS = {
    GRADE_DISCRIMINATED: "판별시험 통과",
    GRADE_INDISCRIMINATE: "무차별 생존",
    GRADE_UNTESTED: "완전 미시험",
    GRADE_UNSTABLE: "불안정",
}
GRADE_LABELS_JA = {
    GRADE_DISCRIMINATED: "判別試験を通過",
    GRADE_INDISCRIMINATE: "無差別に生存",
    GRADE_UNTESTED: "未試験",
    GRADE_UNSTABLE: "不安定",
}
GRADE_LABELS_ZH = {
    GRADE_DISCRIMINATED: "通过判别试验",
    GRADE_INDISCRIMINATE: "无差别存活",
    GRADE_UNTESTED: "完全未测试",
    GRADE_UNSTABLE: "不稳定",
}
# en은 등급 식별자를 그대로 노출한다 (manifest와 같은 어휘).
GRADE_LABELS_BY_LANG = {"ko": GRADE_LABELS, "ja": GRADE_LABELS_JA, "zh": GRADE_LABELS_ZH}

SOURCE_ELICITED = "elicited"
SOURCE_MINED_PRIOR = "mined-prior"

RECHECK_CLASS_PRIORITY = ("unstable", "untested-prior", "conflict")

# 채굴 prior 순위 - DEFAULT_CATALOG 튜플 순서를 내림차순 prior로 해석한다.
# index 0 이 커뮤니티 최빈값(채굴 모드)이며, 생존값이 여러 개면 이 순위가 높은 값을 방출한다.
MINED_PRIOR: dict[str, tuple[str, ...]] = {
    axis: tuple(values) for axis, values in DEFAULT_CATALOG.items()
}

RULE_TEXT: dict[tuple[str, str], str] = {
    ("autonomy", "ask_first"): "코드를 수정하기 전에 계획을 제시하고 승인을 받는다. 명백한 오타 수정처럼 자명한 변경만 예외다.",
    ("autonomy", "propose_then_act"): "짧은 계획을 먼저 적고 곧바로 이어서 실행한다.",
    ("autonomy", "act_then_report"): "먼저 실행하고 변경 내역을 요약해 보고한다.",
    ("commit_style", "conventional"): "커밋 메시지는 feat/fix/refactor 같은 conventional prefix로 시작한다. 리포에 이미 자리잡은 커밋 컨벤션이 있으면 그것을 우선한다.",
    ("commit_style", "narrative"): "커밋 메시지 제목은 변경 의도를 서술형 문장으로 적는다. 리포에 이미 자리잡은 커밋 컨벤션이 있으면 그것을 우선한다.",
    ("commit_style", "no_auto_commit"): "요청받지 않은 커밋은 만들지 않는다. 변경은 워킹 트리에 남겨 사용자가 확인 후 직접 커밋하게 한다.",
    ("test_discipline", "test_first"): "버그 수정은 재현 테스트를 먼저 작성해 실패를 확인한 뒤 고친다. 기능 구현도 테스트를 먼저 쓴다.",
    ("test_discipline", "test_after"): "구현 직후 같은 변경 안에서 테스트를 추가한다. 버그 수정도 같다: 먼저 고치고 같은 변경에서 회귀 테스트를 붙이며 실패하는 테스트를 먼저 쓰지 않는다. 테스트 없는 변경을 완료로 선언하지 않는다.",
    ("test_discipline", "on_request"): "테스트는 명시적으로 요청받았을 때만 작성한다. 단, 기존 테스트가 깨지는지 확인은 항상 한다.",
    ("comment_doc", "minimal"): "주석은 코드로 표현할 수 없는 제약과 이유에만 남긴다. 코드가 하는 일을 다시 서술하는 주석은 쓰지 않는다.",
    ("comment_doc", "docstring_only"): "공개 함수와 클래스에는 docstring을 쓰고, 인라인 주석은 남기지 않는다.",
    ("comment_doc", "thorough"): "공개 API에는 docstring을 쓰고, 비자명한 로직에만 인라인 주석을 남긴다.",
    ("error_behavior", "stop_and_report"): "에러가 나면 즉시 멈추고 원문 로그 그대로 보고한다. 로그를 요약하거나 가공하지 않는다.",
    ("error_behavior", "retry_then_report"): "일시적으로 보이는 실패는 한 번만 재시도한다. 그래도 실패하면 원문 로그와 함께 보고하고 멈춘다.",
    ("error_behavior", "self_heal"): "테스트나 빌드 실패는 원인을 고쳐 통과할 때까지 진행한 뒤 결과를 보고한다.",
    ("scope_adherence", "strict"): "요청받은 범위 밖의 파일은 수정하지 않는다. 범위 밖 결함을 발견하면 고치지 말고 보고만 한다.",
    ("scope_adherence", "adjacent_fix_ok"): "요청 범위와 직접 맞닿은 결함까지만 같은 변경에서 고치고, 그 사실을 보고에 명시한다. 그 밖의 발견은 보고만 한다.",
    ("scope_adherence", "proactive"): "작업 중 발견한 개선점은 같은 변경에 포함하되, 요청 범위와 부수 정리를 보고에서 구분해 적는다. diff가 요청보다 커지면 사전에 알린다.",
    ("verification", "always_run"): "완료를 선언하기 전에 테스트와 빌드를 실제로 돌려 통과 출력을 확인한다.",
    ("verification", "on_risky"): "위험한 변경일 때만 전체 검증을 돌리고, 평소에는 변경과 직접 관련된 테스트만 확인한다.",
    ("verification", "trust_static"): "정적 확인(코드 재독, 타입 체크)으로 충분하다고 판단되면 그대로 완료를 선언한다.",
    ("dependency_policy", "prefer_existing"): "새 패키지보다 기존 의존성과 표준 라이브러리를 우선한다. 불가피하게 추가하면 그 사실을 보고한다.",
    ("dependency_policy", "ask_first"): "새 의존성은 추가하기 전에 반드시 확인을 받는다.",
    ("dependency_policy", "free"): "필요한 의존성은 바로 추가하고, 추가한 목록을 보고에 남긴다.",
}

#: 맥락 분기 규칙의 되돌리기-어려운-작업 절 - 앞에 조건 접두가 붙는다.
IRREVERSIBLE_CLAUSE: dict[tuple[str, str], str] = {
    ("autonomy", "ask_first"): "실행 전에 반드시 승인을 받는다",
    ("autonomy", "propose_then_act"): "계획을 알린 뒤 진행하되 최종 적용은 승인을 기다린다",
    ("autonomy", "act_then_report"): "먼저 실행하고 결과를 보고한다",
    ("error_behavior", "stop_and_report"): "에러 즉시 멈추고 원문 로그로 보고한다",
    ("error_behavior", "retry_then_report"): "일시적 실패만 한 번 재시도하고 이후에는 멈춰 보고한다",
    ("error_behavior", "self_heal"): "복구 로직을 넣어 끝까지 진행한 뒤 결과를 보고한다",
    ("verification", "always_run"): "적용 전에 사본 리허설과 롤백 검증까지 실제로 돌린다",
    ("verification", "on_risky"): "전체 검증과 리허설을 반드시 돌린다",
    ("verification", "trust_static"): "정적 확인만으로 완료를 선언한다",
    ("dependency_policy", "prefer_existing"): "기존 의존성만으로 해결한다",
    ("dependency_policy", "ask_first"): "새 도구 설치 전에 반드시 확인을 받는다",
    ("dependency_policy", "free"): "필요한 도구를 바로 설치해 진행한다",
    ("commit_style", "conventional"): "커밋은 만들되 리포 컨벤션을 따른다",
    ("commit_style", "narrative"): "커밋 제목은 변경 의도를 서술형으로 적는다",
    ("commit_style", "no_auto_commit"): "커밋을 만들지 않고 변경을 워킹 트리에 남긴다",
}

IRREVERSIBLE_CONDITION_PREFIX = (
    "단, 삭제, push, 배포, 마이그레이션처럼 되돌리기 어려운 작업에서는 "
)

RULE_TEXT_EN: dict[tuple[str, str], str] = {
    ("autonomy", "ask_first"): "Present a plan and get approval before modifying code. The only exception is self-evident changes like obvious typo fixes.",
    ("autonomy", "propose_then_act"): "Write a short plan first, then proceed immediately.",
    ("autonomy", "act_then_report"): "Act first, then report a summary of what changed.",
    ("commit_style", "conventional"): "Start commit messages with a conventional prefix like feat/fix/refactor. If the repo already has an established commit convention, that convention wins.",
    ("commit_style", "narrative"): "Write commit titles as narrative sentences describing the intent of the change. If the repo already has an established commit convention, that convention wins.",
    ("commit_style", "no_auto_commit"): "Never create commits that were not requested. Leave changes in the working tree for the user to review and commit themselves.",
    ("test_discipline", "test_first"): "For bug fixes, write a reproducing test first and watch it fail before fixing. For features, write the tests first too.",
    ("test_discipline", "test_after"): "Add tests in the same change right after implementing. This applies to bug fixes too: fix first, then add the regression test in the same change - do not write the failing test first. Never declare a change done without tests.",
    ("test_discipline", "on_request"): "Write tests only when explicitly asked. Always check whether existing tests break, though.",
    ("comment_doc", "minimal"): "Leave comments only for constraints and reasons the code cannot express. Never write comments that restate what the code does.",
    ("comment_doc", "docstring_only"): "Write docstrings on public functions and classes; leave no inline comments.",
    ("comment_doc", "thorough"): "Write docstrings on public APIs and leave inline comments only on non-obvious logic.",
    ("error_behavior", "stop_and_report"): "On error, stop immediately and report the raw log verbatim. Never summarize or massage the log.",
    ("error_behavior", "retry_then_report"): "Retry a transient-looking failure exactly once. If it still fails, report with the raw log and stop.",
    ("error_behavior", "self_heal"): "On test or build failures, fix the cause and keep going until they pass, then report the outcome.",
    ("scope_adherence", "strict"): "Never modify files outside the requested scope. If you find a defect outside the scope, report it without fixing it.",
    ("scope_adherence", "adjacent_fix_ok"): "Fix defects directly adjacent to the requested scope in the same change and say so in the report. Anything further out gets reported only.",
    ("scope_adherence", "proactive"): "Include improvements discovered during the work in the same change, but separate the requested scope from incidental cleanup in the report. If the diff grows beyond the request, flag it beforehand.",
    ("verification", "always_run"): "Before declaring done, actually run the tests and the build and confirm the passing output.",
    ("verification", "on_risky"): "Run full verification only for risky changes; otherwise check just the tests directly related to the change.",
    ("verification", "trust_static"): "When static checks (re-reading the code, type checking) seem sufficient, declare done on that basis.",
    ("dependency_policy", "prefer_existing"): "Favor existing dependencies and the standard library over new packages. If an addition is unavoidable, report it.",
    ("dependency_policy", "ask_first"): "Always ask before adding a new dependency.",
    ("dependency_policy", "free"): "Add whatever dependencies are needed right away, and list the additions in the report.",
}

IRREVERSIBLE_CLAUSE_EN: dict[tuple[str, str], str] = {
    ("autonomy", "ask_first"): "always get approval before executing",
    ("autonomy", "propose_then_act"): "announce the plan and proceed, but wait for approval on the final apply",
    ("autonomy", "act_then_report"): "act first and report the result",
    ("error_behavior", "stop_and_report"): "stop on error immediately and report with the raw log",
    ("error_behavior", "retry_then_report"): "retry only transient failures once, then stop and report",
    ("error_behavior", "self_heal"): "add recovery logic, run through to the end, then report the result",
    ("verification", "always_run"): "actually run a copy rehearsal and a rollback check before applying",
    ("verification", "on_risky"): "always run full verification and a rehearsal",
    ("verification", "trust_static"): "declare done on static checks alone",
    ("dependency_policy", "prefer_existing"): "solve it with existing dependencies only",
    ("dependency_policy", "ask_first"): "always ask before installing any new tool",
    ("dependency_policy", "free"): "install whatever tools are needed and proceed",
    ("commit_style", "conventional"): "create commits but follow the repo convention",
    ("commit_style", "narrative"): "write commit titles as narrative intent",
    ("commit_style", "no_auto_commit"): "create no commits and leave changes in the working tree",
}

IRREVERSIBLE_CONDITION_PREFIX_EN = (
    "However, for hard-to-reverse work like deletes, pushes, deploys, and migrations, "
)


RULE_TEXT_JA: dict[tuple[str, str], str] = {
    ("autonomy", "ask_first"): "コードを変更する前に計画を示し、承認を得る。明らかなタイポ修正のような自明な変更だけが例外。",
    ("autonomy", "propose_then_act"): "短い計画を先に書き、そのまま実行に進む。",
    ("autonomy", "act_then_report"): "先に実行し、変更内容を要約して報告する。",
    ("commit_style", "conventional"): "コミットメッセージは feat/fix/refactor のような conventional prefix で始める。リポジトリに既存のコミット規約があればそちらを優先する。",
    ("commit_style", "narrative"): "コミットの題名は変更の意図を叙述文で書く。リポジトリに既存のコミット規約があればそちらを優先する。",
    ("commit_style", "no_auto_commit"): "頼まれていないコミットは作らない。変更はワーキングツリーに残し、ユーザーが確認して自分でコミットする。",
    ("test_discipline", "test_first"): "バグ修正は再現テストを先に書いて失敗を確認してから直す。機能実装もテストを先に書く。",
    ("test_discipline", "test_after"): "実装の直後、同じ変更の中でテストを追加する。バグ修正も同じで、先に直してから同じ変更で回帰テストを足し、失敗するテストを先に書かない。テストのない変更を完了と宣言しない。",
    ("test_discipline", "on_request"): "テストは明示的に頼まれたときだけ書く。ただし既存テストが壊れていないかの確認は必ず行う。",
    ("comment_doc", "minimal"): "コメントはコードで表せない制約と理由にだけ残す。コードのしていることを言い直すコメントは書かない。",
    ("comment_doc", "docstring_only"): "公開関数とクラスには docstring を書き、インラインコメントは残さない。",
    ("comment_doc", "thorough"): "公開 API には docstring を書き、自明でないロジックにだけインラインコメントを残す。",
    ("error_behavior", "stop_and_report"): "エラーが出たらすぐ止め、生のログをそのまま報告する。ログを要約したり加工したりしない。",
    ("error_behavior", "retry_then_report"): "一時的に見える失敗は一度だけ再試行する。それでも失敗したら生のログと一緒に報告して止める。",
    ("error_behavior", "self_heal"): "テストやビルドの失敗は原因を直して通るまで進め、その後に結果を報告する。",
    ("scope_adherence", "strict"): "依頼された範囲外のファイルは変更しない。範囲外の欠陥を見つけたら直さずに報告だけする。",
    ("scope_adherence", "adjacent_fix_ok"): "依頼範囲に直接隣接する欠陥までは同じ変更で直し、その事実を報告に明記する。それ以外の発見は報告だけする。",
    ("scope_adherence", "proactive"): "作業中に見つけた改善点は同じ変更に含めるが、報告では依頼範囲と付随的な整理を分けて書く。diff が依頼より大きくなるなら事前に知らせる。",
    ("verification", "always_run"): "完了を宣言する前にテストとビルドを実際に走らせ、通った出力を確認する。",
    ("verification", "on_risky"): "リスクの高い変更のときだけ全体検証を走らせ、普段は変更に直接関係するテストだけ確認する。",
    ("verification", "trust_static"): "静的な確認（コードの読み直し、型チェック）で十分と判断したら、そのまま完了を宣言する。",
    ("dependency_policy", "prefer_existing"): "新しいパッケージより既存の依存関係と標準ライブラリを優先する。やむを得ず追加したらその事実を報告する。",
    ("dependency_policy", "ask_first"): "新しい依存関係は追加する前に必ず確認を取る。",
    ("dependency_policy", "free"): "必要な依存関係はすぐ追加し、追加した一覧を報告に残す。",
}

IRREVERSIBLE_CLAUSE_JA: dict[tuple[str, str], str] = {
    ("autonomy", "ask_first"): "実行前に必ず承認を得る",
    ("autonomy", "propose_then_act"): "計画を知らせてから進めるが、最終適用は承認を待つ",
    ("autonomy", "act_then_report"): "先に実行して結果を報告する",
    ("error_behavior", "stop_and_report"): "エラーが出たらすぐ止めて生のログで報告する",
    ("error_behavior", "retry_then_report"): "一時的な失敗だけ一度再試行し、その後は止めて報告する",
    ("error_behavior", "self_heal"): "復旧ロジックを入れて最後まで進めてから結果を報告する",
    ("verification", "always_run"): "適用前にコピーでのリハーサルとロールバック検証まで実際に走らせる",
    ("verification", "on_risky"): "全体検証とリハーサルを必ず走らせる",
    ("verification", "trust_static"): "静的な確認だけで完了を宣言する",
    ("dependency_policy", "prefer_existing"): "既存の依存関係だけで解決する",
    ("dependency_policy", "ask_first"): "新しいツールを入れる前に必ず確認を取る",
    ("dependency_policy", "free"): "必要なツールをすぐ入れて進める",
    ("commit_style", "conventional"): "コミットは作るがリポジトリの規約に従う",
    ("commit_style", "narrative"): "コミットの題名は変更の意図を叙述文で書く",
    ("commit_style", "no_auto_commit"): "コミットは作らず変更をワーキングツリーに残す",
}

IRREVERSIBLE_CONDITION_PREFIX_JA = (
    "ただし、削除・push・デプロイ・マイグレーションのような取り消しにくい作業では、"
)

RULE_TEXT_ZH: dict[tuple[str, str], str] = {
    ("autonomy", "ask_first"): "修改代码之前先给出方案并获得批准。只有像明显拼写错误这类不言自明的改动可以例外。",
    ("autonomy", "propose_then_act"): "先写一个简短的方案，然后直接开始执行。",
    ("autonomy", "act_then_report"): "先执行，再汇报一份改动摘要。",
    ("commit_style", "conventional"): "提交信息以 feat/fix/refactor 之类的 conventional 前缀开头。如果仓库已有约定俗成的提交规范，以仓库规范为准。",
    ("commit_style", "narrative"): "提交标题用叙述句写出改动的意图。如果仓库已有约定俗成的提交规范，以仓库规范为准。",
    ("commit_style", "no_auto_commit"): "绝不创建没有被要求的提交。把改动留在工作区，由用户检查后自己提交。",
    ("test_discipline", "test_first"): "修 bug 先写复现测试并看着它失败，再动手修。做功能也先写测试。",
    ("test_discipline", "test_after"): "实现之后立刻在同一次改动里补测试。修 bug 也一样：先修好，再在同一次改动里补回归测试，不要先写失败的测试。没有测试的改动不算完成。",
    ("test_discipline", "on_request"): "只在被明确要求时才写测试。但一定要检查现有测试有没有被弄坏。",
    ("comment_doc", "minimal"): "注释只留给代码表达不了的约束和理由。不要写复述代码在做什么的注释。",
    ("comment_doc", "docstring_only"): "公开函数和类写 docstring，不留行内注释。",
    ("comment_doc", "thorough"): "公开 API 写 docstring，只在不显而易见的逻辑处留行内注释。",
    ("error_behavior", "stop_and_report"): "出错就立刻停下，把原始日志原样报出来。不要摘要或加工日志。",
    ("error_behavior", "retry_then_report"): "看起来是临时性的失败只重试一次。还失败就连同原始日志一起汇报并停下。",
    ("error_behavior", "self_heal"): "测试或构建失败就修掉原因，一直做到通过，然后汇报结果。",
    ("scope_adherence", "strict"): "绝不修改请求范围之外的文件。发现范围外的缺陷只汇报、不修。",
    ("scope_adherence", "adjacent_fix_ok"): "只把与请求范围直接相邻的缺陷放在同一次改动里修掉，并在汇报中说明。更远的发现只汇报。",
    ("scope_adherence", "proactive"): "工作中发现的改进点放进同一次改动，但在汇报里把请求范围和顺手整理分开写。如果 diff 比请求大，提前说明。",
    ("verification", "always_run"): "宣布完成之前，实际跑一遍测试和构建，确认通过的输出。",
    ("verification", "on_risky"): "只对有风险的改动跑完整验证；平时只检查与改动直接相关的测试。",
    ("verification", "trust_static"): "如果判断静态检查（重读代码、类型检查）已经足够，就据此宣布完成。",
    ("dependency_policy", "prefer_existing"): "优先使用已有依赖和标准库，而不是新包。不得已新增时要汇报。",
    ("dependency_policy", "ask_first"): "新增任何依赖之前一定先确认。",
    ("dependency_policy", "free"): "需要的依赖直接加上，并在汇报里列出新增清单。",
}

IRREVERSIBLE_CLAUSE_ZH: dict[tuple[str, str], str] = {
    ("autonomy", "ask_first"): "执行前一定先获得批准",
    ("autonomy", "propose_then_act"): "先告知方案再推进，但最终应用要等批准",
    ("autonomy", "act_then_report"): "先执行再汇报结果",
    ("error_behavior", "stop_and_report"): "出错立刻停下并用原始日志汇报",
    ("error_behavior", "retry_then_report"): "只对临时性失败重试一次，之后停下汇报",
    ("error_behavior", "self_heal"): "加入恢复逻辑跑到最后，再汇报结果",
    ("verification", "always_run"): "应用前实际做完副本演练和回滚检查",
    ("verification", "on_risky"): "一定跑完整验证和演练",
    ("verification", "trust_static"): "只凭静态检查宣布完成",
    ("dependency_policy", "prefer_existing"): "只用已有依赖解决",
    ("dependency_policy", "ask_first"): "安装任何新工具前一定先确认",
    ("dependency_policy", "free"): "需要什么工具就直接装上继续",
    ("commit_style", "conventional"): "创建提交但遵循仓库规范",
    ("commit_style", "narrative"): "提交标题用叙述句写出意图",
    ("commit_style", "no_auto_commit"): "不创建提交，把改动留在工作区",
}

IRREVERSIBLE_CONDITION_PREFIX_ZH = (
    "不过，对于删除、push、部署、迁移这类难以撤销的工作，"
)

#: 언어별 규칙 문안 테이블 - 이벤트 원장은 언어 중립이고 언어는 컴파일 시점 렌더 선택이다.
# 언어별 문장 결합: (기본 규칙과 조건절 사이의 접합 문자열, 문장 종결 부호).
SENTENCE_STYLE = {
    "ko": (" ", "."),
    "en": (" ", "."),
    "ja": ("", "。"),
    "zh": ("", "。"),
}

RULE_LANG_TABLES: dict[str, tuple[dict[tuple[str, str], str], dict[tuple[str, str], str], str]] = {
    "ko": (RULE_TEXT, IRREVERSIBLE_CLAUSE, IRREVERSIBLE_CONDITION_PREFIX),
    "en": (RULE_TEXT_EN, IRREVERSIBLE_CLAUSE_EN, IRREVERSIBLE_CONDITION_PREFIX_EN),
    "ja": (RULE_TEXT_JA, IRREVERSIBLE_CLAUSE_JA, IRREVERSIBLE_CONDITION_PREFIX_JA),
    "zh": (RULE_TEXT_ZH, IRREVERSIBLE_CLAUSE_ZH, IRREVERSIBLE_CONDITION_PREFIX_ZH),
}

DEFAULT_RULE_LANG = "ko"

# XOUT.md 본문에 절대 실려서는 안 되는 인식론 어휘.
EPISTEMIC_TOKENS: tuple[str, ...] = (
    "corroboration",
    "corroborated",
    "grade",
    "untested",
    "unstable",
    "mined-prior",
    "mined prior",
    "elicited",
    "provenance",
    "n=0",
    "미시험",
    "무차별",
    "불안정",
    "판별시험",
    "반증",
    "추측",
    "가설",
    "prior",
    "TODO",
)


class CompileViolation(ValueError):
    """컴파일 입력이 스키마를 위반했을 때."""


class HashMismatch(RuntimeError):
    """마지막 쓰기 content hash와 디스크 현재 내용이 다를 때 - silent overwrite 금지."""

    def __init__(self, records: Sequence[Mapping[str, Any]]) -> None:
        self.records = tuple(dict(record) for record in records)
        paths = ", ".join(str(record.get("path")) for record in self.records)
        super().__init__(f"content hash mismatch: {paths}")


@dataclass(frozen=True, slots=True)
class CompiledRule:
    """XOUT.md 한 줄과 manifest 한 항목이 공유하는 단일 룰."""

    axis: str
    value: str
    text: str
    corroboration_grade: str
    value_source: str
    surviving: tuple[str, ...]
    eliminated: tuple[str, ...]
    provenance: tuple[str, ...] = ()
    pair_struck: bool = False
    demoted: bool = False
    irreversible_value: str | None = None

    @property
    def rule_id(self) -> str:
        return f"{CATALOG_VERSION}:{self.axis}:{self.value}"

    @property
    def grade_label(self) -> str:
        return GRADE_LABELS[self.corroboration_grade]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "axis": self.axis,
            "value": self.value,
            "rule": self.text,
            "catalog_version": CATALOG_VERSION,
            "corroboration_grade": self.corroboration_grade,
            "corroboration_label": self.grade_label,
            "value_source": self.value_source,
            "surviving_values": list(self.surviving),
            "eliminated_values": list(self.eliminated),
            "refutation_provenance": list(self.provenance),
            "pair_struck": self.pair_struck,
            "demoted_to_untested": self.demoted,
            "irreversible_value": self.irreversible_value,
        }


@dataclass(frozen=True, slots=True)
class WriteResult:
    """write_outputs 반환값 - 착지 경로와 방출된 manifest."""

    base_dir: Path
    manifest: dict[str, Any]
    written: tuple[Path, ...] = ()
    mismatches: tuple[dict[str, Any], ...] = field(default=())

    def path(self, name: str) -> Path:
        return self.base_dir / name


def default_base_dir() -> Path:
    """xout 단독 소유 산출 디렉터리."""
    return Path.home() / ".claude" / "xout"


def _now(now: str | None = None) -> str:
    if now is not None:
        return now
    return datetime.now(timezone.utc).isoformat()


def content_hash(payload: str | bytes) -> str:
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def mined_prior_rank(axis: str, value: str) -> int:
    """작을수록 강한 채굴 prior. 카탈로그에 없는 값은 맨 뒤로 민다."""
    order = MINED_PRIOR.get(axis, ())
    if value in order:
        return order.index(value)
    return len(order) + 1


def mined_mode(axis: str) -> str:
    """축의 채굴 최빈값 - 반증 이력 0건 축이 방출하는 값."""
    order = MINED_PRIOR.get(axis)
    if not order:
        raise CompileViolation(f"unknown axis: {axis}")
    return order[0]


def select_value(axis: str, surviving: Sequence[str]) -> str:
    """생존값 중 채굴 prior가 가장 강한 값.

    생존 1개면 그 값, 생존 2개면 prior 상위값, 반증 0건(생존 3개)이면 채굴 최빈값으로
    세 갈래가 하나의 규칙으로 수렴한다.
    """
    if not surviving:
        return mined_mode(axis)
    return min(surviving, key=lambda value: (mined_prior_rank(axis, value), value))


def _probe_flip_axes(events: Iterable[Any]) -> frozenset[str]:
    flipped: set[str] = set()
    for event in events:
        if getattr(event, "type", None) is not EventType.PROBE_RESULT:
            continue
        payload = getattr(event, "payload", None) or {}
        result = str(payload.get("result", "")).strip().lower()
        if result != "flip" and payload.get("flip") is not True:
            continue
        axis = payload.get("axis")
        if axis:
            flipped.add(str(axis))
    return frozenset(flipped)


def _strike_scan(events: Iterable[Any]) -> tuple[frozenset[str], dict[str, list[str]]]:
    """pair-strike만 받은 축 후보와 축별 반증 provenance를 원본 스트림에서 긁어온다."""
    pair_struck: set[str] = set()
    provenance: dict[str, list[str]] = {}
    for event in events:
        if getattr(event, "type", None) is not EventType.STRIKE:
            continue
        if not getattr(event, "has_discriminating_power", False):
            axis = getattr(event, "axis", None)
            if axis:
                pair_struck.add(str(axis))
            continue
        for refutation in getattr(event, "refutations", ()):
            provenance.setdefault(str(refutation.axis), []).append(str(event.event_id))
    return frozenset(pair_struck), provenance


def _grade(
    axis: str,
    discrimination: str,
    *,
    flipped: bool,
    pair_struck: bool,
) -> str:
    if flipped:
        return GRADE_UNSTABLE
    if discrimination == "complete":
        return GRADE_DISCRIMINATED
    if discrimination == "partial":
        return GRADE_INDISCRIMINATE
    if pair_struck:
        return GRADE_INDISCRIMINATE
    return GRADE_UNTESTED


def _context_stream(
    stream: Sequence[Any], context: str
) -> tuple[Any, ...]:
    """맥락 클래스에 속한 긋기만 남긴다 - 비긋기 이벤트(undo/revive)는 양쪽에 남는다."""
    kept: list[Any] = []
    for event in stream:
        if isinstance(event, StrikeEvent):
            if SCENE_CONTEXTS.get(event.scene_id, CONTEXT_ROUTINE) == context:
                kept.append(event)
        else:
            kept.append(event)
    return tuple(kept)


def _lang_tables(
    lang: str,
) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str], str]:
    tables = RULE_LANG_TABLES.get(lang)
    if tables is None:
        raise CompileViolation(f"unsupported rule language: {lang!r}")
    return tables


def conditional_rule_text(
    axis: str,
    routine_value: str,
    irreversible_value: str,
    lang: str = DEFAULT_RULE_LANG,
) -> str:
    """두 맥락의 생존값이 갈릴 때 - 평시 문장 + 되돌리기-어려운-작업 절."""
    rule_text, clauses, prefix = _lang_tables(lang)
    clause = clauses[(axis, irreversible_value)]
    joiner, full_stop = SENTENCE_STYLE.get(lang, SENTENCE_STYLE[DEFAULT_RULE_LANG])
    return f"{rule_text[(axis, routine_value)]}{joiner}{prefix}{clause}{full_stop}"


def compile_rules(
    events: Iterable[Any],
    catalog: AxisCatalog | None = None,
    lang: str = DEFAULT_RULE_LANG,
) -> tuple[CompiledRule, ...]:
    """이벤트 스트림에서 8축 전부의 실행 룰을 파생한다(순수 함수, 항상 8개)."""
    rule_text, _, _ = _lang_tables(lang)
    stream = tuple(events)
    active = dict(catalog) if catalog is not None else dict(DEFAULT_CATALOG)
    state = fold(stream, catalog)
    routine_state = fold(_context_stream(stream, CONTEXT_ROUTINE), catalog)
    irreversible_state = fold(_context_stream(stream, CONTEXT_IRREVERSIBLE), catalog)
    flipped_axes = _probe_flip_axes(stream)
    pair_struck_axes, provenance = _strike_scan(stream)

    rules: list[CompiledRule] = []
    for axis in sorted(active):
        axis_state = state.axis(axis)
        surviving = tuple(axis_state.surviving)
        value = select_value(axis, surviving)
        routine_axis = routine_state.axis(axis)
        irreversible_axis = irreversible_state.axis(axis)
        divergent: str | None = None
        if routine_axis.eliminated and irreversible_axis.eliminated:
            routine_value = select_value(axis, tuple(routine_axis.surviving))
            irreversible_value = select_value(
                axis, tuple(irreversible_axis.surviving)
            )
            if routine_value != irreversible_value:
                value = routine_value
                divergent = irreversible_value
                text = conditional_rule_text(
                    axis, routine_value, irreversible_value, lang
                )
            else:
                value = routine_value
                text = rule_text.get((axis, value))
        else:
            text = rule_text.get((axis, value))
        if text is None:
            raise CompileViolation(f"no executable rule text for {axis}={value}")
        pair_struck = axis in pair_struck_axes
        grade = _grade(
            axis,
            axis_state.effective_discrimination,
            flipped=axis in flipped_axes,
            pair_struck=pair_struck,
        )
        source = SOURCE_ELICITED if axis_state.eliminated else SOURCE_MINED_PRIOR
        rules.append(
            CompiledRule(
                axis=axis,
                value=value,
                text=text,
                corroboration_grade=grade,
                value_source=source,
                surviving=surviving,
                eliminated=tuple(axis_state.eliminated),
                provenance=tuple(provenance.get(axis, ())),
                pair_struck=pair_struck,
                demoted=bool(axis_state.demoted),
                irreversible_value=divergent,
            )
        )
    return tuple(rules)


#: XOUT.md의 고정 골격 - 프리앰블(우선순위), 두 섹션, 애매할 때의 판단 규칙.
#: 에이전트가 읽는 문서라 인식론 어휘는 쓰지 않는다 (EPISTEMIC_TOKENS 가드).
XOUT_DOC: dict[str, dict[str, str]] = {
    "ko": {
        "preamble": "당신이 일하는 사람의 고정 선호다. 구체적인 대안 두 개를 보고 본인이 직접 고른 것이다. 프로젝트 자체의 CLAUDE.md나 AGENTS.md와 정면으로 충돌하면 프로젝트 쪽이 이기고, 그 외에는 이 규칙을 따른다.",
        "routine": "## 일상 작업",
        "irreversible": "## 되돌리기 어려운 작업",
        "intro": "삭제, push, 배포, 마이그레이션, 그리고 명령 하나로 되돌릴 수 없는 모든 작업. **중요: 되돌리기 어려운 작업인지 애매하면 어려운 쪽으로 취급한다.** 이 작업에서는 아래 줄이 위 섹션의 같은 항목을 대체한다.",
        "rejected": "사용자가 지운 것",
    },
    "en": {
        "preamble": "Standing preferences of the person you are working for, chosen by them between two concrete alternatives. A project's own CLAUDE.md or AGENTS.md wins on a direct conflict; otherwise follow these.",
        "routine": "## Routine work",
        "irreversible": "## Hard-to-reverse work",
        "intro": "Deletes, pushes, deploys, migrations, and anything else one command cannot undo. **IMPORTANT: when unsure whether work is hard to reverse, treat it as hard to reverse.** For this work the lines below replace their counterparts above.",
        "rejected": "the user rejected",
    },
    "ja": {
        "preamble": "あなたが一緒に働く人の固定された好みです。具体的な二つの選択肢を見て本人が選んだものです。プロジェクト自身の CLAUDE.md や AGENTS.md と正面から衝突する場合はプロジェクト側が優先し、それ以外はこの規則に従ってください。",
        "routine": "## 日常作業",
        "irreversible": "## 取り消しにくい作業",
        "intro": "削除、push、デプロイ、マイグレーション、そしてコマンド一つでは元に戻せないすべての作業。**重要: 取り消しにくい作業かどうか迷ったら、取り消しにくい側として扱う。** この作業では下の行が上のセクションの同じ項目を置き換える。",
        "rejected": "ユーザーが消したもの",
    },
    "zh": {
        "preamble": "这是与你共事的人的固定偏好，由本人在两个具体备选之间亲自选定。若与项目自身的 CLAUDE.md 或 AGENTS.md 正面冲突，以项目为准；其余情况遵循这些规则。",
        "routine": "## 日常工作",
        "irreversible": "## 难以撤销的工作",
        "intro": "删除、push、部署、迁移，以及任何一条命令无法撤销的工作。**重要：拿不准是否难以撤销时，按难以撤销处理。** 对这类工作，下面的条目替换上一节中的对应条目。",
        "rejected": "用户划掉的",
    },
}


def render_xout_md(rules: Sequence[CompiledRule], lang: str = DEFAULT_RULE_LANG) -> str:
    """에이전트가 읽는 문서 - 우선순위 프리앰블, 일상/되돌리기-어려운 두 섹션, 인식론 주석 0줄.

    조건은 한 번만 정의하고 규칙마다 반복하지 않는다. 각 규칙에는 사용자가
    실제로 지운 대안을 짧게 붙여 규칙이 무엇을 배제하는지 알린다.
    """
    doc = XOUT_DOC.get(lang) or XOUT_DOC[DEFAULT_RULE_LANG]
    rule_text, clauses, _ = _lang_tables(lang)
    _, full_stop = SENTENCE_STYLE.get(lang, SENTENCE_STYLE[DEFAULT_RULE_LANG])
    lines = ["# xout Rules", "", doc["preamble"], "", doc["routine"], ""]
    for rule in rules:
        text = rule_text[(rule.axis, rule.value)]
        rejected = rejected_values(rule)
        if rejected:
            text = f"{text} ({doc['rejected']}: {', '.join(rejected)})"
        lines.append(f"- {text}")
    conditional = [rule for rule in rules if rule.irreversible_value]
    if conditional:
        from xout.state import axis_label  # 순환 import 회피 - 호출 시점에만

        lines.extend(["", doc["irreversible"], "", doc["intro"], ""])
        for rule in conditional:
            clause = clauses[(rule.axis, rule.irreversible_value)]
            clause = clause[:1].upper() + clause[1:]
            lines.append(f"- {axis_label(rule.axis, lang)}: {clause}{full_stop}")
    return "\n".join(lines) + "\n"


def rejected_values(rule: CompiledRule) -> tuple[str, ...]:
    """규칙 본문에 붙는 '사용자가 지운 것' - 두 맥락 어디에서도 살아남지 못한 값만."""
    kept = {rule.value, rule.irreversible_value}
    return tuple(v for v in rule.eliminated if v not in kept)


def render_settings(rules: Sequence[CompiledRule]) -> str:
    """제안 파일 - 라이브 settings.json에 자동 병합하지 않는다."""
    payload = {
        "_popper": {
            "proposal_only": True,
            "catalog_version": CATALOG_VERSION,
            "scope": SCOPE,
            "rules_ref": XOUT_MD,
        },
        "recommended_hooks": [],
        "rule_ids": [rule.rule_id for rule in rules],
    }
    return _canonical(payload)


def _recheck_queue(rules: Sequence[CompiledRule], conflicts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for rule in rules:
        if rule.corroboration_grade == GRADE_UNSTABLE:
            queue.append({"class": "unstable", "axis": rule.axis, "rule_id": rule.rule_id})
    for rule in rules:
        if rule.corroboration_grade == GRADE_UNSTABLE:
            continue
        if rule.demoted or rule.corroboration_grade == GRADE_UNTESTED:
            queue.append({"class": "untested-prior", "axis": rule.axis, "rule_id": rule.rule_id})
    for conflict in conflicts:
        entry = {"class": "conflict"}
        entry.update(dict(conflict))
        queue.append(entry)
    for index, entry in enumerate(queue):
        entry["priority"] = RECHECK_CLASS_PRIORITY.index(str(entry["class"]))
        entry["order"] = index
    return queue


def build_manifest(
    rules: Sequence[CompiledRule],
    *,
    documents: Mapping[str, str],
    session_id: str | None = None,
    now: str | None = None,
    conflicts: Sequence[Mapping[str, Any]] = (),
    prereg_ref: str = DEFAULT_PREREG_REF,
    remaining_combinations: int | None = None,
    eliminated_pairs: int | None = None,
) -> dict[str, Any]:
    """사이드카 manifest - 인식론 메타데이터, 파일 단위 content hash, last_review, scope."""
    stamp = _now(now)
    outputs: dict[str, Any] = {}
    for name in OUTPUT_FILES:
        if name == MANIFEST_JSON:
            continue
        body = documents.get(name)
        if body is None:
            continue
        outputs[name] = {"content_hash": content_hash(body), "bytes": len(body.encode("utf-8"))}
    outputs[MANIFEST_JSON] = {"content_hash": None, "self_excluding": True}

    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "catalog_version": CATALOG_VERSION,
        "metric_spec_version": METRIC_SPEC_VERSION,
        "scope": SCOPE,
        "session_id": session_id,
        "generated_at": stamp,
        "last_review": stamp,
        "prereg_ref": prereg_ref,
        "remaining_combinations": remaining_combinations,
        "eliminated_pairs": eliminated_pairs,
        "rules": [rule.to_dict() for rule in rules],
        "outputs": outputs,
        "conflicts": [dict(conflict) for conflict in conflicts],
        "recheck_queue": _recheck_queue(rules, conflicts),
    }
    manifest["outputs"][MANIFEST_JSON]["content_hash"] = manifest_self_hash(manifest)
    return manifest


def manifest_self_hash(manifest: Mapping[str, Any]) -> str:
    """manifest 자기 참조 해시 - outputs['manifest.json'].content_hash 필드를 제외하고 계산한다."""
    shadow = json.loads(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    entry = shadow.get("outputs", {}).get(MANIFEST_JSON)
    if isinstance(entry, dict):
        entry.pop("content_hash", None)
    return content_hash(_canonical(shadow))


def _recorded_hash(manifest: Mapping[str, Any], name: str) -> str | None:
    entry = manifest.get("outputs", {}).get(name)
    if not isinstance(entry, Mapping):
        return None
    recorded = entry.get("content_hash")
    return str(recorded) if recorded else None


def verify_outputs(base_dir: Path) -> tuple[dict[str, Any], ...]:
    """디스크 현재 내용과 manifest에 적힌 마지막 쓰기 hash를 대조한다.

    반환값은 hash_mismatch 이벤트 payload 목록(축 귀속 없음, arity 0)이다.
    """
    manifest_path = base_dir / MANIFEST_JSON
    if not manifest_path.exists():
        return ()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("manifest 파싱 실패: %s", manifest_path, exc_info=True)
        raise HashMismatch(
            [{"path": str(manifest_path), "reason": "unreadable_manifest", "detail": str(exc)}]
        ) from exc

    records: list[dict[str, Any]] = []
    for name in OUTPUT_FILES:
        recorded = _recorded_hash(manifest, name)
        if recorded is None:
            continue
        target = base_dir / name
        if not target.exists():
            records.append({"path": str(target), "reason": "missing", "recorded_hash": recorded})
            continue
        body = target.read_text(encoding="utf-8")
        actual = manifest_self_hash(manifest) if name == MANIFEST_JSON else content_hash(body)
        if name == MANIFEST_JSON:
            try:
                actual = manifest_self_hash(json.loads(body))
            except json.JSONDecodeError as exc:
                raise HashMismatch(
                    [{"path": str(target), "reason": "unreadable_manifest", "detail": str(exc)}]
                ) from exc
        if actual != recorded:
            records.append(
                {
                    "path": str(target),
                    "reason": "manual_edit",
                    "recorded_hash": recorded,
                    "actual_hash": actual,
                }
            )
    return tuple(records)


def _write_outputs_unlocked(
    events: Iterable[Any],
    *,
    catalog: AxisCatalog | None = None,
    base_dir: Path | None = None,
    session_id: str | None = None,
    now: str | None = None,
    conflicts: Sequence[Mapping[str, Any]] = (),
    prereg_ref: str = DEFAULT_PREREG_REF,
    acknowledge_mismatch: bool = False,
    lang: str = DEFAULT_RULE_LANG,
) -> WriteResult:
    """~/.claude/popper/ 안에만 XOUT.md + manifest.json + settings.xout.json을 착지시킨다."""
    target_dir = Path(base_dir) if base_dir is not None else default_base_dir()

    mismatches = verify_outputs(target_dir) if target_dir.exists() else ()
    if mismatches and not acknowledge_mismatch:
        logger.warning("content hash 불일치 - silent overwrite 금지: %s", mismatches)
        raise HashMismatch(mismatches)

    stream = tuple(events)
    rules = compile_rules(stream, catalog, lang)
    state = fold(stream, catalog)

    popper_md = render_xout_md(rules, lang)
    settings = render_settings(rules)
    manifest = build_manifest(
        rules,
        documents={XOUT_MD: popper_md, SETTINGS_JSON: settings},
        session_id=session_id,
        now=now,
        conflicts=conflicts,
        prereg_ref=prereg_ref,
        remaining_combinations=state.remaining_combinations,
        eliminated_pairs=state.eliminated_pairs,
    )
    if mismatches:
        manifest["hash_mismatch_records"] = [dict(record) for record in mismatches]
        # 기록 주입 후 자기 해시를 재계산해야 디스크 내용과 hash 대조가 다시 성립한다.
        manifest["outputs"][MANIFEST_JSON]["content_hash"] = manifest_self_hash(manifest)

    target_dir.mkdir(parents=True, exist_ok=True)
    documents = {
        XOUT_MD: popper_md,
        SETTINGS_JSON: settings,
        MANIFEST_JSON: _canonical(manifest),
    }
    written_by_name: dict[str, Path] = {}
    # manifest를 마지막에 교체해 세 파일 세대의 commit marker로 사용한다.
    for name in (XOUT_MD, SETTINGS_JSON, MANIFEST_JSON):
        path = target_dir / name
        atomic_write_text(path, documents[name])
        written_by_name[name] = path
    logger.info("xout 산출물 착지: %s", target_dir)
    return WriteResult(
        base_dir=target_dir,
        manifest=manifest,
        written=tuple(written_by_name[name] for name in OUTPUT_FILES),
        mismatches=tuple(mismatches),
    )


def write_outputs(
    events: Iterable[Any],
    *,
    catalog: AxisCatalog | None = None,
    base_dir: Path | None = None,
    session_id: str | None = None,
    now: str | None = None,
    conflicts: Sequence[Mapping[str, Any]] = (),
    prereg_ref: str = DEFAULT_PREREG_REF,
    acknowledge_mismatch: bool = False,
    lang: str = DEFAULT_RULE_LANG,
) -> WriteResult:
    """누적 스트림 판독부터 세 파일 착지까지 프로세스 간 단일 트랜잭션으로 실행한다."""
    target_dir = Path(base_dir) if base_dir is not None else default_base_dir()
    with base_lock(target_dir):
        return _write_outputs_unlocked(
            events,
            catalog=catalog,
            base_dir=target_dir,
            session_id=session_id,
            now=now,
            conflicts=conflicts,
            prereg_ref=prereg_ref,
            acknowledge_mismatch=acknowledge_mismatch,
            lang=lang,
        )
