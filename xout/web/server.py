"""표준 라이브러리만 쓰는 로컬 단일 페이지 서버.

경로는 넷뿐이다.

  GET  /        완성된 단일 페이지. 첫 응답에 이미 첫 페어가 박혀 있다.
  GET  /state   현재 페어 + 가설 카운터 + 컴파일된 룰 JSON.
  POST /strike  긋기 한 건을 받고 갱신된 같은 JSON을 되돌려준다.
  POST /undo    마지막 긋기를 undo_tombstone 명시 채널로 무른다(AC3).

승인/확정 경로는 존재하지 않는다. 가설 공간을 움직이는 유일한 동사는 긋기이고,
undo는 오긋기 복구의 명시 이벤트 채널이지 승인이 아니다. 슬롯 캡이 닫힌
세션의 긋기는 409로 기각된다 - 자동 연장은 없다.

루프백 주소에만 바인딩하며 외부로 나가는 요청은 한 건도 만들지 않는다.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

from xout.events import SchemaViolation
from xout.web.page import render_page
from xout.web.state import (
    ColdOpenSession,
    RecoveryUnavailable,
    SessionComplete,
    StalePresentation,
)

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
EPHEMERAL_PORT = 0

PATH_INDEX = "/"
PATH_STATE = "/state"
PATH_STRIKE = "/strike"
PATH_UNDO = "/undo"

CONTENT_HTML = "text/html; charset=utf-8"
CONTENT_JSON = "application/json; charset=utf-8"

#: 긋기 본문은 대상 이름 하나가 전부라 이보다 커질 이유가 없다.
MAX_BODY_BYTES = 4096


def _loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ColdOpenServer(ThreadingHTTPServer):
    """세션 하나를 들고 있는 로컬 서버."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        session: ColdOpenSession,
        *,
        shutdown_on_complete: bool = True,
    ) -> None:
        self.session = session
        self.shutdown_on_complete = shutdown_on_complete
        self.shutdown_requested = Event()
        self._shutdown_guard = Lock()
        super().__init__(server_address, handler_class)

    @property
    def url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        return f"http://{host}:{port}/"

    def request_shutdown(self) -> None:
        """완료 응답을 보낸 요청 스레드 밖에서 serve_forever를 한 번만 종료한다."""
        if not self.shutdown_on_complete or self.shutdown_requested.is_set():
            return
        with self._shutdown_guard:
            if self.shutdown_requested.is_set():
                return
            self.shutdown_requested.set()
            Thread(
                target=self.shutdown,
                name=f"xout-shutdown-{self.server_address[1]}",
                daemon=True,
            ).start()


class ColdOpenHandler(BaseHTTPRequestHandler):
    """긋기와 무름만 받는 요청 핸들러."""

    server_version = "XoutColdOpen"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if not self._request_origin_allowed():
            self._send_json(HTTPStatus.MISDIRECTED_REQUEST, {"error": "untrusted_origin"})
            return
        session = self._session()
        if self.path == PATH_INDEX:
            body = render_page(session.snapshot()).encode("utf-8")
            self._send(HTTPStatus.OK, CONTENT_HTML, body)
            return
        if self.path == PATH_STATE:
            self._send_json(HTTPStatus.OK, session.snapshot().to_dict())
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown_path", "path": self.path})

    def do_POST(self) -> None:
        if not self._request_origin_allowed():
            self._send_json(HTTPStatus.MISDIRECTED_REQUEST, {"error": "untrusted_origin"})
            return
        if self.path == PATH_STRIKE:
            self._handle_strike()
            return
        if self.path == PATH_UNDO:
            self._handle_undo()
            return
        self._send_json(
            HTTPStatus.NOT_FOUND, {"error": "unknown_path", "path": self.path}
        )

    def _handle_strike(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as e:
            logger.warning("긋기 본문을 해석하지 못했다", exc_info=True)
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "malformed_body", "detail": str(e)}
            )
            return

        target = payload.get("target")
        pair_id = payload.get("pair_id")
        slot = payload.get("slot")
        try:
            snapshot = self._session().strike(
                str(target),
                expected_pair_id=str(pair_id) if pair_id is not None else None,
                expected_slot=slot if isinstance(slot, int) else None,
            )
        except SchemaViolation as e:
            logger.warning("긋기 대상이 스키마를 위반했다: %r", target, exc_info=True)
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "unknown_target", "detail": str(e)}
            )
            return
        except StalePresentation as e:
            self._send_json(
                HTTPStatus.CONFLICT,
                {"error": "stale_presentation", "detail": str(e)},
            )
            return
        except SessionComplete as e:
            self._send_json(
                HTTPStatus.CONFLICT, {"error": "session_complete", "detail": str(e)}
            )
            return
        self._send_json(HTTPStatus.OK, snapshot.to_dict())
        if snapshot.session_complete:
            self.server.request_shutdown()

    def _handle_undo(self) -> None:
        try:
            snapshot = self._session().undo()
        except RecoveryUnavailable as e:
            self._send_json(
                HTTPStatus.CONFLICT, {"error": "nothing_to_undo", "detail": str(e)}
            )
            return
        except SessionComplete as e:
            self._send_json(
                HTTPStatus.CONFLICT, {"error": "session_complete", "detail": str(e)}
            )
            return
        self._send_json(HTTPStatus.OK, snapshot.to_dict())

    def log_message(self, format: str, *args: Any) -> None:
        """접근 로그를 stderr 대신 모듈 로거로 흘린다."""
        logger.debug("%s - %s", self.address_string(), format % args)

    def _session(self) -> ColdOpenSession:
        return self.server.session

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length 헤더가 없다")
        try:
            length = int(raw_length)
        except ValueError as e:
            raise ValueError(f"Content-Length가 정수가 아니다: {raw_length!r}") from e
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError(f"본문 길이가 허용 범위를 벗어났다: {length}")
        try:
            document = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f"JSON 본문이 아니다: {e}") from e
        if not isinstance(document, dict):
            raise ValueError("본문 최상위는 객체여야 한다")
        return document

    def _request_origin_allowed(self) -> bool:
        """DNS rebinding과 교차 출처 POST를 루프백 origin 경계에서 거부한다."""
        expected_port = self.server.server_address[1]

        def trusted(raw: str) -> bool:
            authority = raw.split("://", 1)[-1].split("/", 1)[0]
            if not authority or "@" in authority:
                return False
            if authority.startswith("["):
                closing = authority.find("]")
                if closing < 0:
                    return False
                hostname = authority[1:closing]
                raw_port = authority[closing + 1 :]
                if raw_port and not raw_port.startswith(":"):
                    return False
                port_text = raw_port[1:]
            else:
                hostname, separator, port_text = authority.rpartition(":")
                if not separator:
                    hostname, port_text = authority, ""
                elif ":" in hostname:
                    return False
            if not _loopback_host(hostname):
                return False
            if not port_text:
                return True
            return port_text.isdecimal() and int(port_text) == expected_port

        host = self.headers.get("Host")
        if host is None or not trusted(host):
            return False
        origin = self.headers.get("Origin")
        return origin is None or trusted(origin)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, CONTENT_JSON, body)

    def _send(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'none'",
        )
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)


def build_server(
    session: ColdOpenSession | None = None,
    host: str = HOST,
    port: int = EPHEMERAL_PORT,
    repo_root: Path | str | None = None,
    shutdown_on_complete: bool = True,
    **session_kwargs: Any,
) -> ColdOpenServer:
    """바인딩까지 끝난 서버를 돌려준다 - 세션이 없으면 여기서 콜드 오픈한다."""
    if not _loopback_host(host):
        raise ValueError(f"루프백 밖 서버 바인딩은 허용되지 않는다: {host!r}")
    active = (
        session
        if session is not None
        else ColdOpenSession(repo_root=repo_root, **session_kwargs)
    )
    server = ColdOpenServer(
        (host, port),
        ColdOpenHandler,
        active,
        shutdown_on_complete=shutdown_on_complete,
    )
    logger.info("콜드 오픈 서버 대기: %s", server.url)
    return server


def serve(
    host: str = HOST,
    port: int = EPHEMERAL_PORT,
    repo_root: Path | str | None = None,
    session: ColdOpenSession | None = None,
    **session_kwargs: Any,
) -> None:
    """서버를 열고 인터럽트가 올 때까지 요청을 받는다."""
    server = build_server(
        session=session, host=host, port=port, repo_root=repo_root, **session_kwargs
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("콜드 오픈 서버를 닫는다")
    finally:
        server.shutdown()
        server.server_close()
