"""`python -m xout.web` - 로컬 긋기 UI를 연다.

인자 없이 실행하면 루프백의 빈 포트를 잡아 서버를 띄우고 주소를 로그로 남긴다.
기동 과정에서 사용자에게 묻는 것은 하나도 없다.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from xout.web.server import EPHEMERAL_PORT, HOST, serve

logger = logging.getLogger("xout.web")


def main() -> None:
    parser = argparse.ArgumentParser(prog="xout.web", description="로컬 긋기 UI")
    parser.add_argument("--host", default=HOST, help="바인딩할 주소")
    parser.add_argument("--port", type=int, default=EPHEMERAL_PORT, help="바인딩할 포트")
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="슬롯 치환에 쓸 대상 레포 경로. 생략하면 일반 skin을 쓴다",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    serve(host=args.host, port=args.port, repo_root=args.repo)


if __name__ == "__main__":
    main()
