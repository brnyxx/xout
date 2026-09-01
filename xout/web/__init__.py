"""AC8 - 질문 0개로 열리는 로컬 긋기 UI.

서버가 뜨는 순간 첫 페어(자율성 축)가 이미 렌더된 상태로 나오고, 화면의 유일한
입력 어포던스는 긋기다. 긋는 순간 가설 카운터와 컴파일 페인의 룰이 같은
이벤트 스트림에서 다시 파생된다.
"""

from xout.web.page import render_page, render_rules
from xout.web.server import (
    ColdOpenHandler,
    ColdOpenServer,
    PATH_INDEX,
    PATH_STATE,
    PATH_STRIKE,
    build_server,
    serve,
)
from xout.web.state import (
    AXIS_LABELS,
    COLD_OPEN_AXIS,
    STRIKE_LABELS,
    STRIKE_TARGETS,
    ColdOpenSession,
    PairView,
    RuleView,
    Snapshot,
    axis_label,
    ordered_pairs,
)

__all__ = [
    "AXIS_LABELS",
    "COLD_OPEN_AXIS",
    "ColdOpenHandler",
    "ColdOpenServer",
    "ColdOpenSession",
    "PATH_INDEX",
    "PATH_STATE",
    "PATH_STRIKE",
    "PairView",
    "RuleView",
    "STRIKE_LABELS",
    "STRIKE_TARGETS",
    "Snapshot",
    "axis_label",
    "build_server",
    "ordered_pairs",
    "render_page",
    "render_rules",
    "serve",
]
