"""로컬 채굴 - 이미 갖고 있는 에이전트 규칙 파일에서 축 관측을 읽는다.

`xout mine`은 로컬 레포의 CLAUDE.md / AGENTS.md / .cursorrules 류 파일을
읽기전용으로 스캔해, 각 줄을 8축 카탈로그의 값으로 귀속시키고 file:line
영수증과 함께 보고한다. 아무것도 쓰지 않고 원장에도 기록하지 않는다 -
세션에서 X를 칠 때 자기 환경과 교차 확인하는 용도의 관측 보고서다.

귀속은 투명한 키워드 휴리스틱이다: 패턴 테이블이 이 파일에 그대로 있고,
모든 관측은 매칭된 원문 줄을 증거로 동반한다. 휴리스틱이 놓친 줄은
관측이 없는 것이지 선호가 없는 것이 아니다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

from xout.counter import DEFAULT_CATALOG

logger = logging.getLogger(__name__)

#: 스캔 대상 규칙 파일 - 이름 그대로의 파일.
RULE_FILE_NAMES: tuple[str, ...] = (
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    ".cursorrules",
)

#: 스캔 대상 규칙 파일 - 루트 기준 상대 경로.
RULE_FILE_PATHS: tuple[str, ...] = (
    ".github/copilot-instructions.md",
)

#: 스캔 대상 규칙 디렉토리 - 안의 .md/.mdc 파일을 모두 읽는다.
RULE_DIR_PATHS: tuple[str, ...] = (
    ".cursor/rules",
    ".claude/rules",
)

_SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {"node_modules", "__pycache__", "venv", ".venv", "dist", "build", "target"}
)

_MAX_DEPTH = 4
_MAX_FILES = 200
_MAX_LINE_CHARS = 240

#: 한 줄이 같은 축의 값 둘에 걸릴 때 이기는 쪽 - 더 구체적인 주장이 먼저다.
#: ("tests only when asked"는 on_request, "retry once then stop and report"는 retry_then_report,
#: "docstring만 쓰고 인라인 주석은 없다"는 minimal이 아니라 docstring_only,
#: "새 의존성은 물어보고 추가"는 prefer_existing이 아니라 ask_first다.)
#: 표에 없는 축은 애매한 줄을 버린다.
PRECEDENCE: dict[str, tuple[str, ...]] = {
    "test_discipline": ("on_request", "test_first", "test_after"),
    "error_behavior": ("retry_then_report", "self_heal", "stop_and_report"),
    "comment_doc": ("thorough", "docstring_only", "minimal"),
    "dependency_policy": ("ask_first", "free", "prefer_existing"),
}

#: 축 밖 문맥 - 의존성 이야기를 하는 줄은 autonomy로 읽지 않는다.
#: ("새 패키지는 추가 전에 물어봐라"는 의존성 정책이지 자율성 정책이 아니다.)
_DEPENDENCY_SCOPE: tuple[str, ...] = (
    r"dependenc",
    r"\bpackages?\b",
    r"\blibrar(y|ies)\b",
    r"\b(npm|pnpm|yarn|pip|cargo|brew|apt|go get) ?(install|add|i)?\b",
    r"의존성",
    r"패키지",
    r"라이브러리",
    r"依存",
    r"パッケージ",
    r"ライブラリ",
    r"依赖",
    r"(新|装)包",
)

#: (축, 값) -> 매칭 패턴. 정밀도 우선 - 애매한 줄은 귀속하지 않는 편을 택한다.
#: 측정된다: tests/data/mine_corpus.json이 4개 언어 라벨 코퍼스이고
#: tests/test_mine_corpus.py가 축별 정밀도/재현율 바닥을 지킨다.
MINED_PATTERNS: dict[tuple[str, str], tuple[str, ...]] = {
    ("autonomy", "ask_first"): (
        r"ask (for |me for |my )?(approval|permission)",
        r"ask (me )?first",
        r"(run|clear|check) (the |your |it )?(plan|change|approach|this)? ?(by|with|past) me",
        r"before (touching|starting|doing) anything",
        r"(confirm|check) with (me|the user|us)",
        r"(confirm|ask) before",
        r"\b(get|obtain|need|require|wait for) (my |your |an |explicit |prior |the )*"
        r"(approval|permission|sign[- ]?off|go[- ]?ahead|green light)",
        r"(do not|don't|never) (start|proceed|act|edit|change|touch|run|apply)"
        r"[^.]{0,40}without (my |explicit |prior |written )*"
        r"(approval|permission|sign[- ]?off|asking|consent)",
        r"승인(을|은|이)? ?(받|얻|구)",
        r"허락(을|은|이)? ?(받|얻|구)",
        r"확인(을|은)? ?받",
        r"먼저 (물어|여쭤|여쭈|확인)",
        r"(승인|허락|확인|동의)[^.]{0,4}없이[^.]{0,25}(말|않|금지|마라|마세|마십)",
        r"(승인|허락|동의|컨펌)[^.]{0,12}(뒤|후|나면|받으면|떨어지면)[^.]{0,10}(진행|시작|착수)",
        r"承認(を|は)?(得|取|もら)",
        r"許可(を|は)?(得|取|もら)",
        r"(始める|変更する|触る|進める|着手する|書き換える)前に[^。]{0,14}(確認|承認|許可|相談)",
        r"必ず(確認|承認|許可)を(取|得|もら)",
        r"(まず|先に|事前に)[^。]{0,12}相談(して|を取)",
        r"勝手に[^。]{0,20}(進めない|変更しない|触らない|実装しない|やらない)",
        r"(先|事先|提前|之前)[^。]{0,4}(问|征得|请示|报备|确认一下|跟我确认)",
        r"(经过|获得|拿到|征得)[^。]{0,4}(同意|批准|许可|授权)",
        r"未经(同意|批准|许可|授权)",
        r"(改|动手|执行|开始|修改)[^。]{0,6}前[^。]{0,4}(先|要|必须|得)(问|征|确认|批准)",
    ),
    ("autonomy", "propose_then_act"): (
        r"(plan|proposal) first",
        r"(plan|proposal|approach|outline)[^.]{0,25}\bfirst\b[^.]{0,30}\bthen\b",
        r"(write|share|post|sketch|give me|show me)[^.]{0,25}(plan|outline)[^.]{0,30}then",
        r"(propose|outline)[^.]{0,30}then",
        r"계획(을|은)? ?먼저",
        r"(계획|방향|방침|접근)[^.]{0,10}(을|를)? ?(먼저|우선)[^.]{0,12}(적|쓰|세우|정리|공유|잡)",
        r"(계획|방향|방침)[^.]{0,15}(정리|공유|적)[^.]{0,10}(뒤|후|다음)[^.]{0,12}(진행|구현|실행|이어)",
        r"(先に|まず)[^。]{0,14}(計画|方針|プラン|案)[^。]{0,16}(書|立て|示し|共有|まとめ)",
        r"(計画|方針|案)[^。]{0,12}(まとめ|示し|書い)て(から|、)[^。]{0,12}(実装|実行|進|着手)",
        r"先(写|说|列|给出)[^。]{0,10}(方案|计划|思路|提纲)",
        r"(方案|计划|思路)[^。]{0,10}(再|然后)[^。]{0,8}(动手|实现|执行|改|做)",
    ),
    ("autonomy", "act_then_report"): (
        r"\bact first\b",
        r"(just )?(do it|go ahead|proceed)[^.]{0,30}(then |and )?(report|summar|tell me|let me know)",
        r"(never|don't|do not|no need to) (ask|check|confirm)\w*( me| us)?"
        r"( for (permission|approval))?( before| first)\b",
        r"(never|don't|do not|no need to) ask for (permission|approval)",
        r"without (asking|checking in|confirming|waiting for approval)",
        r"(report|summarize|tell me)[^.]{0,25}(afterwards|after the fact|when (you're|it's) done)",
        r"먼저 (실행|진행|작업|고치|손대)",
        r"(묻지|물어보지|기다리지) ?말고",
        r"(승인|확인|허락)[^.]{0,6}(기다리지|받지) ?(말|않)",
        r"(?<!곧)바로 (진행|실행|고치|작업|손대)",
        r"(실행|작업|수정)[^.]{0,8}(뒤|후에?|다음)[^.]{0,12}(보고|알려|정리해)",
        r"先に(実行|作業|直し|やっ|進め)",
        r"自己判断で(進め|やっ|直し|実装)",
        r"(いちいち)?(聞かず|確認せず|承認を待たず|許可を待たず)",
        r"(実行|やっ|進め)て(から|、)[^。]{0,12}(あとで|後で)?[^。]{0,6}報告",
        r"先(做|干|改|执行|动手)[^。]{0,14}(再|然后|完了|做完|干完)[^。]{0,10}(汇报|报告|说|通知)",
        r"直接(动手|开干|改|做)",
        r"不(用|需要)(先)?(问|请示|确认|等)",
        r"别问",
        r"(做完|干完|改完)[^。]{0,6}(再|后)[^。]{0,4}(汇报|报告|说)",
    ),
    ("commit_style", "conventional"): (
        r"conventional commit",
        r"\b(feat|fix|chore|refactor|docs|test|perf)\s*:",
        r"\b(feat|fix|chore|refactor)\s*/\s*(feat|fix|chore|refactor|docs|test)\b",
        r"commit (message|title|subject)s?[^.]{0,40}prefix",
        r"커밋 ?(메시지|제목|타이틀)[^.]{0,30}(prefix|접두|컨벤션|conventional)",
        r"커밋[^.]{0,20}(conventional|컨벤셔널)",
        r"コミット[^。]{0,20}(prefix|接頭|規約|conventional)",
        r"提交[^。]{0,15}(前缀|prefix|规范|conventional)",
    ),
    ("commit_style", "narrative"): (
        r"narrative commit",
        r"commit (message|title|subject)s?[^.]{0,45}(sentence|prose|plain english|what changed)",
        r"(sentence|prose|narrative)[^.]{0,30}commit (message|title|subject)",
        r"(skip|drop|no|without) (the )?prefix(es)?[^.]{0,35}commit",
        r"commit (title|subject|message)s?[^.]{0,30}(without|no) (a )?prefix",
        r"서술형",
        r"커밋 ?(제목|메시지|타이틀)[^.]{0,30}(문장|서술)",
        r"(접두사|prefix)[^.]{0,10}없이[^.]{0,25}(커밋|제목)",
        r"커밋[^.]{0,20}(접두사|prefix)[^.]{0,8}없이",
        r"コミット[^。]{0,18}(件名|メッセージ|タイトル)[^。]{0,18}(文章|一文|文で)",
        r"コミット[^。]{0,20}(prefix|接頭辞)[^。]{0,8}なし",
        r"提交(标题|信息|标题|说明)[^。]{0,18}(一句|句子|完整的句)",
        r"(不加|不用|去掉)(前缀|prefix)",
    ),
    ("commit_style", "no_auto_commit"): (
        r"(do not|don't|never|no)\s+(auto[- ]?)?commit",
        r"(do not|don't|never) run git commit",
        r"commit only (when|if|after)",
        r"leave (the |your )?changes in the working tree",
        r"커밋(을|은)? ?(만들지|하지|남기지) ?(않|말)",
        r"(요청|지시)[^.]{0,12}(않은|않으면|없이)[^.]{0,12}커밋",
        r"커밋[^.]{0,15}(워킹 ?트리|작업 ?트리)",
        r"(頼まれ|言われ)て(い)?ないコミット",
        r"コミット(は|を)?(作らない|しない|せず)",
        r"(コミット|commit)[^。]{0,16}(実行しない|しないこと|するな|禁止)",
        r"変更は?ワーキングツリーに残",
        r"(不要|别|不用)(自己)?(跑 )?(git )?(提交|commit)",
        r"没(让|人|有)[^。]{0,8}(要求)?[^。]{0,4}就?不(要|用)提交",
        r"改动(留|放)在工作区",
    ),
    ("test_discipline", "test_first"): (
        r"tests?[- ]first",
        r"\bTDD\b",
        r"(failing|reproducing|repro) test[^.]{0,20}(first|before)",
        r"(write|add)[^.]{0,25}(failing|reproducing) test",
        r"tests? before[^.]{0,20}(the )?(implementation|code|fix)",
        r"(bug ?)?fix(es)? start from a[^.]{0,20}test",
        r"테스트를? 먼저",
        r"재현 테스트",
        r"먼저[^.]{0,12}(실패하는|깨지는) 테스트",
        r"(실패하는|깨지는|재현) 테스트(부터|를 먼저)",
        r"(テストを|テストは)?先に(失敗する)?テストを?(書|作)",
        r"失敗するテストを(先に)?書",
        r"テストを?先に書",
        r"先写[^。]{0,12}(会失败|失败)的?测试",
        r"(测试|test)先行",
        r"先写测试",
        r"(复现|重现)测试",
    ),
    ("test_discipline", "test_after"): (
        r"(?<!to )\b(add|write|include|attach)\s+(the |a |unit |integration |regression )*tests?\b",
        r"tests? (are |is )?(required|mandatory|not optional)",
        r"\bwith tests\b",
        r"(needs?|need) tests",
        r"(no|never a) change[^.]{0,25}without tests",
        r"(not |isn't |is not )done without tests",
        r"테스트(를|도|는)? ?(추가|붙|동반|함께|같이)",
        r"테스트(가|는|를)? ?(필수|필요)",
        r"테스트 없(는|이)[^.]{0,20}(완료|끝|머지|커밋|배포)",
        r"(구현|수정)[^.]{0,15}(직후|같은 변경|같은 커밋)[^.]{0,15}테스트",
        r"テストを(追加|付け|足す|書く)",
        r"テストが(必要|必須)",
        r"テストのない[^。]{0,15}(完了|マージ)",
        r"(补|加|带)上?测试",
        r"(都|要|需要)(带|写)测试",
    ),
    ("test_discipline", "on_request"): (
        r"tests? only (when|if|on request)",
        r"only (write|add)[^.]{0,20}tests?[^.]{0,20}(when|if)",
        r"(write|add)[^.]{0,20}tests? only",
        r"tests? (are |is )?optional",
        r"tests?[^.]{0,25}(unless|only when|only if)[^.]{0,20}(asked|requested|i ask)",
        r"테스트는?[^.]{0,20}(요청|지시|시킬|말할)[^.]{0,10}(때|경우)만",
        r"요청[^.]{0,15}테스트",
        r"(요청|지시)하지 않으면[^.]{0,20}테스트",
        r"テストは[^。]{0,20}(頼まれ|言われ|指示され)[^。]{0,10}(とき|時)だけ",
        r"(指示|依頼)がなけれ[^。]{0,15}テスト",
        r"テスト[^。]{0,15}(だけ書|書かなくて)",
        r"(只有|只在)[^。]{0,15}(要求|让|说)[^。]{0,8}才写?(测试)",
        r"没(要求|说|提)[^。]{0,10}就?不(用|需要)写?测试",
        r"测试[^。]{0,12}(只在|只有)[^。]{0,10}要求",
    ),
    ("comment_doc", "minimal"): (
        r"(avoid|no|minimal|minimi[sz]e|unnecessary|fewer|less)\s+\w*\s*comments?",
        r"(don't|do not|never) (write|add|leave|put)[^.]{0,25}comments?",
        r"comments?[^.]{0,20}(to a|at a|the) minimum",
        r"self[- ]document",
        r"comments? only (for|when|where)",
        r"comments? that (restate|repeat|describe what)",
        r"주석[^.]{0,15}(금지|최소|지양|줄인|줄이)",
        r"주석(은|을|도)? ?(쓰지|남기지|달지|넣지) ?(않|말|마)",
        r"불필요한 주석",
        r"코드가 (스스로|알아서)",
        r"コメント[^。]{0,14}(最小限|書かない|残さない|つけない|不要)",
        r"(不要な|余計な)コメント",
        r"コードをなぞる",
        r"注释[^。]{0,12}(尽量少|最少|少写|不用写)",
        r"(少|别多)(写|放|加)[^。]{0,4}注释",
        r"不(要|用)(写|加)[^。]{0,10}注释",
        r"别(加|写)[^。]{0,10}注释",
        r"代码自己说明",
    ),
    ("comment_doc", "docstring_only"): (
        r"docstring",
        r"\bJSDoc\b",
        r"\bgodoc\b",
    ),
    ("comment_doc", "thorough"): (
        r"(comment|document)\w*\s+(thoroughly|extensively|in detail)",
        r"explain why",
        r"inline comments? (wherever|where|whenever|on|for|in)",
        r"add inline comments",
        r"상세한 주석",
        r"(왜|이유)[^.]{0,20}주석",
        r"주석(으로|을)[^.]{0,10}(충분히|자세히|꼼꼼히)",
        r"(주석|문서)[^.]{0,12}(자세히|꼼꼼히|충분히)",
        r"コメント[^。]{0,12}(丁寧|詳し|しっかり)",
        r"なぜ[^。]{0,20}(コメント|残|書)",
        r"インラインコメントを?[^。]{0,10}(必ず)?(付ける|付け足|残す|書く|入れる)",
        r"注释[^。]{0,10}(详细|写清楚|写全)",
        r"为什么[^。]{0,12}(写|说)清楚",
        r"(都要|要|得)加[^。]{0,8}注释",
    ),
    ("error_behavior", "stop_and_report"): (
        r"stop and (report|ask|show)",
        r"stop (immediately|right away|at once)",
        r"(do not|don't|never) (swallow|hide|suppress|bury)",
        r"raw (error|log|output|traceback|stderr)",
        r"\bverbatim\b",
        r"원문 로그",
        r"(즉시|바로) 멈추",
        r"예외를? ?(삼키|숨기|무시하)",
        r"(로그|출력)(를|을)? ?그대로",
        r"生のログ",
        r"(すぐ|即座に|即)(止め|停止)",
        r"例外を(握りつぶ|飲み込|無視)",
        r"(エラー|ログ|出力)[^。]{0,10}そのまま",
        r"原始日志",
        r"(一)?(出错|失败)(了)?就停",
        r"(吞掉|吞了|吞)异常",
        r"原样(贴|给|报|发)",
    ),
    ("error_behavior", "retry_then_report"): (
        r"retry (it |the command )?(exactly )?once",
        r"retry\b[^.]{0,40}\bonce\b",
        r"one retry",
        r"try (it )?again once",
        r"once,? then (stop|report|hand)",
        r"한 ?번(만|까지)? ?(재시도|다시)",
        r"재시도는 한 ?번",
        r"一度だけ(やり直|再試行|リトライ)",
        r"リトライは一回",
        r"(只)?重试一(次|遍|下)",
        r"重试最多一次",
    ),
    ("error_behavior", "self_heal"): (
        r"until (the |they |it |tests? |the build )?(pass|green|succeed)",
        r"keep (going|fixing|trying|at it) until",
        r"(don't|do not|never) stop (at|on) the first",
        r"work through it until",
        r"통과할 때까지",
        r"(초록|그린)[^.]{0,8}될 때까지",
        r"될 때까지 (계속|끝까지)",
        r"끝까지 (고쳐|진행)",
        r"(通る|パスする|直る)まで",
        r"直し続け",
        r"手を止めない",
        r"一直(修|改|试)[^。]{0,8}到",
        r"直到[^。]{0,10}(通过|全绿|绿)",
        r"(不过|挂了|失败了)就?(接着|继续)改",
    ),
    ("scope_adherence", "strict"): (
        r"only[^.]{0,25}(requested|asked)",
        r"only (change|touch|edit|fix|do)[^.]{0,30}(asked|requested)",
        r"(do not|don't|never) (touch|modify|change|edit)[^.]{0,30}unrelated",
        r"unrelated (files|code|changes|modules)",
        r"stay (in|within|inside) (the )?scope",
        r"out[- ]of[- ]scope",
        r"report[^.]{0,25}instead of fixing",
        r"범위 (밖|외)",
        r"관련 없는 (파일|코드|부분)",
        r"요청(받은)? ?(것|부분|범위)만",
        r"(頼まれ|依頼され)た(もの|範囲|ファイル|コード|部分|こと)[^。]{0,10}(だけ|のみ)",
        r"関係ない(ファイル|コード|部分)",
        r"(指示|依頼)された[^。]{0,12}以外",
        r"範囲(の)?外",
        r"只改[^。]{0,12}(要求|请求|需要|让)",
        r"无关(的)?(文件|代码|部分)",
        r"范围(之)?外",
    ),
    ("scope_adherence", "adjacent_fix_ok"): (
        r"(closely )?related (fix|bug|defect|cleanup|issue)",
        r"right next to (the|your) change",
        r"adjacent",
        r"same change is (fine|ok|okay)",
        r"인접한?",
        r"맞닿",
        r"바로 옆",
        r"すぐ(隣|そば)",
        r"隣接",
        r"(周り|近く|隣)[^。]{0,14}(不具合|問題|バグ|修正)[^。]{0,14}(一緒に|同じ変更|ついでに)",
        r"紧(挨|邻)",
        r"相邻",
        r"顺手(一起)?修",
    ),
    ("scope_adherence", "proactive"): (
        r"refactor as you go",
        r"proactive(ly)?",
        r"(clean|tidy)\w*\s*up[^.]{0,25}(as you go|along the way)",
        r"boy ?scout",
        r"improvements? you (spot|notice|find)",
        r"(fix|improve)[^.]{0,25}(things )?you notice",
        r"선제적",
        r"(발견|눈에 띈|보이는)[^.]{0,20}(개선|정리|리팩)",
        r"그때그때 (리팩|개선|정리)",
        r"지나가다[^.]{0,15}(리팩|개선|정리)",
        r"ついでに[^。]{0,12}(リファクタ|整理|直)",
        r"気づいた[^。]{0,12}(改善|問題|点)[^。]{0,14}(入れ|直し|やっ)",
        r"先回りして",
        r"顺手(重构|整理|清理)",
        r"(发现|看到)[^。]{0,10}改进[^。]{0,10}(一起|主动|直接)",
        r"主动[^。]{0,8}(重构|改掉|整理)",
    ),
    ("verification", "always_run"): (
        r"(run|pass)[^.]{0,30}(tests?|suite|build|lint)[^.]{0,30}before",
        r"before (submitting|committing|declaring|pushing|you commit|you call|calling|you say)",
        r"(must|has to|have to) (pass|be green)",
        r"all tests[^.]{0,20}(pass|green)",
        r"actually run",
        r"(완료|커밋|보고|푸시)[^.]{0,8}전에?[^.]{0,25}(테스트|빌드|검증)",
        r"실제로 (돌려|실행|돌린)",
        r"검증[^.]{0,10}(통과|후에)",
        r"(전체|모든) 테스트를? (돌|실행)",
        r"테스트[^.]{0,10}(전부|모두|다) ?(돌려|실행)",
        r"(完了|コミット|報告|プッシュ)[^。]{0,10}前に[^。]{0,20}(テスト|ビルド|検証)",
        r"実際に(走らせ|実行|動かし)",
        r"必ず(テスト|ビルド)を?(通|走らせ)",
        r"(完成|提交|汇报|发布)[^。]{0,8}前[^。]{0,18}(测试|构建|验证)",
        r"测试[^。]{0,10}(全部通过|全绿|都通过)",
        r"跑[^。]{0,8}测试[^。]{0,8}(再|才)[^。]{0,6}(说|算|宣布|汇报)",
    ),
    ("verification", "on_risky"): (
        r"(full|whole|entire)[^.]{0,25}(suite|verification|tests?)[^.]{0,25}(only )?(for|when|if)"
        r"[^.]{0,20}(risky|dangerous|big|large)",
        r"only[^.]{0,20}(risky|dangerous)[^.]{0,30}(full|whole|entire|everything)",
        r"(just|only) (run )?the (touched|related|affected|relevant) tests",
        r"(related|relevant|touched) tests (is|are) enough",
        r"위험한? ?(변경|작업)[^.]{0,12}(때|경우)만",
        r"(관련|해당)(된|되는)? 테스트만",
        r"(작은|사소한) (수정|변경)[^.]{0,25}(테스트만|만 돌)",
        r"リスク[^。]{0,14}(とき|場合)だけ",
        r"関(係|連)する[^。]{0,10}(テスト)?だけ",
        r"(小さな|軽い)(修正|変更)[^。]{0,20}だけ",
        r"(风险|危险)[^。]{0,12}才[^。]{0,14}(完整|全部|全量|全套)",
        r"只跑(相关|受影响|相应)",
        r"小改动[^。]{0,12}(只跑|只测)",
    ),
    ("verification", "trust_static"): (
        r"type[- ]?check(ing|s)?[^.]{0,25}(is|are) (enough|sufficient|fine)",
        r"static (checks?|analysis|review)[^.]{0,25}(is|are) (enough|sufficient|fine)",
        r"(enough|sufficient)[^.]{0,30}(type[- ]?check|re-?read)",
        r"(re-?read|reading the code)[^.]{0,30}(is|are) enough",
        r"no need to (actually )?run the tests",
        r"정적 확인만",
        r"(타입 ?체크|정적 확인)[^.]{0,20}(충분|만으로)",
        r"(다시 읽|재확인)[^.]{0,20}충분",
        r"型チェック[^。]{0,18}(十分|だけ)",
        r"静的(な)?(確認|チェック)[^。]{0,12}(だけ|で十分)",
        r"(読み直し|目視)[^。]{0,14}十分",
        r"(类型检查|静态检查)[^。]{0,14}(够|足够|就行|就够)",
        r"(过一遍|看一遍)[^。]{0,12}(类型|代码)[^。]{0,10}就(够|行)",
    ),
    ("dependency_policy", "prefer_existing"): (
        r"(prefer|favor|favour|stick to|use)[^.]{0,25}(the )?(standard library|stdlib|built[- ]ins?)",
        r"(prefer|favor|favour|stick to)[^.]{0,25}existing[^.]{0,20}(dependenc|packages?|librar|tool)",
        r"(avoid|no|don't|do not|never|without)[^.]{0,15}(add|adding|introduc|new)[^.]{0,20}dependenc",
        r"(avoid|no|don't|do not|never)[^.]{0,15}new (packages?|librar)",
        r"minimi[sz]e[^.]{0,20}dependenc",
        r"already in (the )?(package\.json|requirements|go\.mod|cargo\.toml|lock ?file|project)",
        r"zero[- ]dependenc",
        r"기존 (의존성|라이브러리|패키지)",
        r"이미 (있는|들어) ?있는[^.]{0,12}(의존성|라이브러리|패키지)",
        r"표준 라이브러리",
        r"새 (의존성|패키지|라이브러리)[^.]{0,20}(추가하지|늘리지|쓰지|않|말|피)",
        r"(既存|標準)[^。]{0,10}(依存|ライブラリ)",
        r"新しい(依存|パッケージ|ライブラリ)[^。]{0,16}(増やさない|入れない|使わない|避け|控え)",
        r"(优先|尽量)[^。]{0,12}(已有|现有|标准库|自带)",
        r"(能用|尽量用)[^。]{0,8}(标准库|自带|已有|现有)",
        r"(不要|别|尽量不要?|少)[^。]{0,6}(引入|加|装)[^。]{0,8}新?的?(依赖|包|库)",
    ),
    ("dependency_policy", "ask_first"): (
        r"(ask|confirm|check|clear it)[^.]{0,30}before[^.]{0,30}(add|install|dependenc|package|librar)",
        r"before (adding|installing|pulling in)[^.]{0,25}(dependenc|package|librar)",
        r"(dependenc|package|librar)\w*[^.]{0,30}(needs?|need|require|requires)"
        r"[^.]{0,20}(approval|permission|sign[- ]?off|my ok)",
        r"(dependenc|package|librar)\w*[^.]{0,25}without (asking|approval|permission)",
        r"(의존성|패키지|라이브러리)[^.]{0,20}(추가|설치)[^.]{0,10}전에[^.]{0,16}(확인|허락|승인|물어)",
        r"(의존성|패키지|라이브러리)[^.]{0,20}(확인|허락|승인)(을|은)? ?(받|구)",
        r"(패키지|의존성|라이브러리)[^.]{0,12}(설치|추가)[^.]{0,12}전[^.]{0,12}(먼저|물어)",
        r"(依存|パッケージ|ライブラリ)[^。]{0,20}(追加|入れる|導入)[^。]{0,10}前に[^。]{0,14}(確認|承認|相談|許可)",
        r"(依赖|包|库)[^。]{0,12}(之)?前[^。]{0,8}(先问|问我|请示|同意|批准)",
        r"(装|加|引入)[^。]{0,8}(新)?(依赖|包|库)[^。]{0,10}(要|得|需要)[^。]{0,6}(先问|同意|批准|请示)",
    ),
    ("dependency_policy", "free"): (
        r"(install|add|pull in)[^.]{0,15}(whatever|any|whichever)[^.]{0,25}(need|necessary|want)",
        r"(no need|don't need|without having) to ask[^.]{0,30}(install|add|dependenc|package)",
        r"free to (add|install|pull in)",
        r"필요한 (의존성|패키지|라이브러리)[^.]{0,16}(바로|그냥|자유|추가)",
        r"(패키지|의존성|라이브러리)[^.]{0,16}(묻지 ?말고|확인 ?없이|물어보지 ?말고)",
        r"必要な(依存|パッケージ|ライブラリ)[^。]{0,16}(その場で|すぐ|自由に|追加してよい|入れてよい)",
        r"(パッケージ|依存|ライブラリ)[^。]{0,16}(確認なし|聞かずに|勝手に入れ)",
        r"(需要|要)(什么|的)?(依赖|包|库)[^。]{0,10}直接(装|加|引入)",
        r"(装|加)(包|依赖|库)[^。]{0,10}不用(问|请示|确认)",
    ),
}

#: (축, 값) -> 그 셀을 무효화하는 패턴. 부정 뒤집기와 다른 축의 문맥을 걸러낸다.
#: ("never ask before acting"는 ask_first가 아니고, "새 패키지는 물어봐라"는
#: autonomy가 아니라 dependency_policy다.)
MINED_VETOES: dict[tuple[str, str], tuple[str, ...]] = {
    ("autonomy", "ask_first"): _DEPENDENCY_SCOPE + (
        r"(never|don't|do not|no need to) (ask|check|confirm)",
        r"without (asking|checking in|confirming)",
        r"(聞かず|確認せず|承認を待たず|許可を待たず)",
        r"不(用|需要)(先)?(问|请示|确认)",
        r"别问",
        r"(묻지|물어보지|기다리지) ?말",
    ),
    ("autonomy", "propose_then_act"): _DEPENDENCY_SCOPE,
    ("autonomy", "act_then_report"): _DEPENDENCY_SCOPE,
    ("test_discipline", "test_after"): (
        r"ask (me|us|the user|the human)\b",
        r"대신 (써|쓰|작성)",
        r"(頼む|お願いする)な",
    ),
    ("comment_doc", "docstring_only"): (
        r"(no|skip|without|don't|do not|never)[^.]{0,15}docstring",
        r"docstring[^.]{0,12}(없이|않|말|금지)",
        r"docstring[^。]{0,12}(なし|書かない|不要)",
        r"(不写|不加|不用写)[^。]{0,6}docstring",
    ),
    ("commit_style", "conventional"): (
        r"(skip|drop|no|without) (the )?prefix",
        r"(접두사|접두어|prefix)[^.]{0,6}(없이|빼|생략)",
        r"(prefix|接頭辞)[^。]{0,6}(なし|付けない|使わない)",
        r"(不加|不用|去掉)(前缀|prefix)",
    ),
    ("dependency_policy", "ask_first"): (
        r"(no need|don't need|never need|without having) to ask",
        r"free to (add|install)",
        r"(묻지|물어보지) ?말",
        r"확인 ?없이",
        r"(確認なし|聞かずに)",
        r"不用(问|请示|确认)",
    ),
    ("dependency_policy", "prefer_existing"): (
        r"dependenc\w* injection",
        r"의존성 주입",
        r"依存性注入",
        r"依赖注入",
    ),
}

_COMPILED: dict[tuple[str, str], tuple[re.Pattern[str], ...]] = {
    key: tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)
    for key, patterns in MINED_PATTERNS.items()
}

_COMPILED_VETOES: dict[tuple[str, str], tuple[re.Pattern[str], ...]] = {
    key: tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)
    for key, patterns in MINED_VETOES.items()
}


def attribute(line: str) -> list[tuple[str, str]]:
    """한 줄을 (축, 값)들로 귀속한다 - 축당 최대 하나, 애매하면 버린다.

    파일 없이 매처만 부르는 진입점이다 - 라벨 코퍼스 측정이 이 함수를 쓴다.
    """
    hits: dict[str, list[str]] = {}
    for key, patterns in _COMPILED.items():
        if not any(pattern.search(line) for pattern in patterns):
            continue
        if any(veto.search(line) for veto in _COMPILED_VETOES.get(key, ())):
            continue
        hits.setdefault(key[0], []).append(key[1])
    attributed: list[tuple[str, str]] = []
    for axis, values in hits.items():
        if len(values) != 1:
            order = PRECEDENCE.get(axis)
            if order is None:
                continue  # 우선순위가 없는 축은 애매한 줄을 버린다
            values = [value for value in order if value in values][:1]
            if not values:
                continue
        attributed.append((axis, values[0]))
    return attributed


@dataclass(frozen=True, slots=True)
class Observation:
    """규칙 파일 한 줄이 한 (축, 값)으로 귀속된 관측 - file:line 영수증 동반."""

    axis: str
    value: str
    path: str
    line_no: int
    line: str
    abs_path: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "axis": self.axis,
            "value": self.value,
            "path": self.path,
            "line": self.line_no,
            "text": self.line,
        }


def _iter_rule_files(root: Path) -> Iterator[Path]:
    """루트 아래의 규칙 파일을 결정적 순서로 낸다 - 얕은 깊이만 걷는다."""
    seen: set[Path] = set()
    count = 0

    def _emit(path: Path) -> Iterator[Path]:
        nonlocal count
        if path in seen or not path.is_file() or count >= _MAX_FILES:
            return
        seen.add(path)
        count += 1
        yield path

    for rel in RULE_FILE_PATHS:
        yield from _emit(root / rel)
    for rel in RULE_DIR_PATHS:
        directory = root / rel
        if directory.is_dir():
            for child in sorted(directory.iterdir()):
                if child.suffix in (".md", ".mdc"):
                    yield from _emit(child)
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if len(relative.parts) > _MAX_DEPTH:
            continue
        if any(
            part in _SKIP_DIR_NAMES
            or (part.startswith(".") and part not in (".cursorrules", ".github"))
            for part in relative.parts[:-1]
        ):
            continue
        if path.name in RULE_FILE_NAMES:
            yield from _emit(path)


def _without_owned_text(text: str) -> str:
    """xout이 직접 쓴 소유 블록과 @import 줄을 비운다 - 줄 번호는 유지.

    자기 산출물을 다시 채굴하면 자기 규칙이 중복·모순으로 보고된다.
    """
    from xout.targets import find_block  # targets는 mine을 import하지 않는다

    while True:
        match = find_block(text)
        if match is None:
            break
        blanked = "".join("\n" if ch == "\n" else " " for ch in match.group(0))
        text = text[: match.start()] + blanked + text[match.end():]
    lines = text.split("\n")
    return "\n".join(
        "" if line.lstrip().startswith("@") and ("XOUT.md" in line or "POPPER.md" in line) else line
        for line in lines
    )


def _observe_file(path: Path, display: str) -> list[Observation]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("규칙 파일을 읽지 못했다: %s", path, exc_info=True)
        return []
    found: list[Observation] = []
    for line_no, raw in enumerate(_without_owned_text(text).splitlines(), start=1):
        line = raw.strip()
        if not line or len(line) > _MAX_LINE_CHARS:
            continue
        for axis, value in attribute(line):
            found.append(
                Observation(
                    axis=axis,
                    value=value,
                    path=display,
                    line_no=line_no,
                    line=line,
                    abs_path=str(path.resolve()),
                )
            )
    return found


#: 사용자 레벨 규칙 - Claude Code가 모든 프로젝트에 읽히는 파일들.
USER_RULE_FILE = ".claude/CLAUDE.md"
USER_RULE_DIR = ".claude/rules"


def user_rule_files(home: Path | None = None) -> list[Path]:
    """`~/.claude/CLAUDE.md`와 `~/.claude/rules/*.md|.mdc` - 존재하는 것만, 결정적 순서."""
    home = (home or Path.home()).expanduser()
    files: list[Path] = []
    claude_md = home / USER_RULE_FILE
    if claude_md.is_file():
        files.append(claude_md)
    rules_dir = home / USER_RULE_DIR
    if rules_dir.is_dir():
        files.extend(
            child for child in sorted(rules_dir.iterdir())
            if child.is_file() and child.suffix in (".md", ".mdc")
        )
    return files


def mine(
    roots: list[Path],
    include_user: bool = False,
    home: Path | None = None,
) -> list[Observation]:
    """루트들을 읽기전용으로 스캔해 축 관측 목록을 낸다 (결정적 순서).

    include_user=True면 사용자 레벨 규칙(`~/.claude/CLAUDE.md`, `~/.claude/rules/`)을
    루트 뒤에 덧붙인다 - 같은 파일이 루트에서 이미 읽혔으면 중복하지 않는다.
    """
    observations: list[Observation] = []
    seen: set[str] = set()
    for root in roots:
        root = root.expanduser()
        if root.is_file():
            seen.add(str(root.resolve()))
            observations.extend(_observe_file(root, str(root)))
            continue
        if not root.is_dir():
            logger.warning("채굴 루트가 없다: %s", root)
            continue
        for path in _iter_rule_files(root):
            seen.add(str(path.resolve()))
            observations.extend(
                _observe_file(path, str(path.relative_to(root)))
            )
    if include_user:
        home_dir = (home or Path.home()).expanduser()
        for path in user_rule_files(home_dir):
            if str(path.resolve()) in seen:
                continue
            observations.extend(
                _observe_file(path, "~/" + path.relative_to(home_dir).as_posix())
            )
    return observations


def summarize(observations: list[Observation]) -> dict[str, dict[str, int]]:
    """축별 값 관측 수 - 카탈로그의 모든 축을 항상 포함한다."""
    counts: dict[str, dict[str, int]] = {
        axis: {value: 0 for value in values}
        for axis, values in DEFAULT_CATALOG.items()
    }
    for observation in observations:
        counts[observation.axis][observation.value] += 1
    return counts


@dataclass(frozen=True, slots=True)
class Conflict:
    """프로젝트 규칙 파일의 한 줄이 컴파일된 규칙과 다른 값을 요구하는 지점."""

    axis: str
    rule_value: str
    observed_value: str
    path: str
    line_no: int
    line: str

    def to_dict(self) -> dict[str, object]:
        return {
            "axis": self.axis,
            "rule_value": self.rule_value,
            "observed_value": self.observed_value,
            "path": self.path,
            "line": self.line_no,
            "text": self.line,
        }


def find_conflicts(
    observations: list[Observation],
    rules: Mapping[str, tuple[str, str | None]],
) -> list[Conflict]:
    """관측값이 두 맥락(일상/되돌리기 어려운 작업) 어느 쪽 생존값도 아니면 충돌이다.

    rules: 축 -> (일상 생존값, 되돌리기-어려운-작업 생존값 또는 None).
    프로젝트 규칙이 이기는 것은 XOUT.md 프리앰블이 이미 말한다 - 여기서는
    어디가 갈리는지 file:line으로 보여 주기만 한다.
    """
    conflicts: list[Conflict] = []
    for obs in observations:
        kept = rules.get(obs.axis)
        if kept is None:
            continue
        if obs.value in {value for value in kept if value}:
            continue
        conflicts.append(
            Conflict(obs.axis, kept[0], obs.value, obs.path, obs.line_no, obs.line)
        )
    return conflicts


@dataclass(frozen=True, slots=True)
class Duplicate:
    """프로젝트/사용자 규칙 파일의 한 줄이 이미 컴파일된 규칙과 같은 값을 말하는 지점."""

    axis: str
    value: str
    path: str
    line_no: int
    line: str
    abs_path: str

    def to_dict(self) -> dict[str, object]:
        return {
            "axis": self.axis,
            "value": self.value,
            "path": self.path,
            "line": self.line_no,
            "text": self.line,
        }


def find_duplicates(
    observations: list[Observation],
    rules: Mapping[str, tuple[str, str | None]],
) -> list[Duplicate]:
    """관측값이 두 맥락 중 한쪽 생존값과 같으면 XOUT.md가 이미 커버하는 줄이다."""
    duplicates: list[Duplicate] = []
    for obs in observations:
        kept = rules.get(obs.axis)
        if kept is None or obs.value not in {value for value in kept if value}:
            continue
        duplicates.append(
            Duplicate(obs.axis, obs.value, obs.path, obs.line_no, obs.line, obs.abs_path)
        )
    return duplicates
