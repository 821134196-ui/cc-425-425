"""零依赖 HTTP 服务：静态页面 + JSON API + 本地识别证据下载。

启动::

    python -m parking.server            # 默认 8000 端口
    PARKING_PORT=9000 python -m parking.server
"""
from __future__ import annotations

import json
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import auth, config, queries
from .db import connect, init_db
from .payment import InMemoryPaymentAdapter
from .service import (ANOMALY_LABELS, RELEASE_REASONS, ParkingService,
                      ServiceError, NotFound)
from .seed import seed

# 进程内共享的模拟支付通道（跨请求保留扣款/退款流水）
ADAPTER = InMemoryPaymentAdapter()
TOKENS: dict[str, auth.Operator] = {}


def _service() -> tuple[ParkingService, object]:
    conn = connect()
    return ParkingService(conn, ADAPTER), conn


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Handler(BaseHTTPRequestHandler):
    server_version = "ParkingReview/1.0"

    # ------------------------------------------------------------ 基础工具
    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ApiError(400, f"请求体不是合法 JSON：{exc}")

    def _current_operator(self) -> auth.Operator:
        token = self.headers.get("X-Auth-Token", "")
        op = TOKENS.get(token)
        if op is None:
            raise ApiError(401, "未登录或会话已失效")
        return op

    def log_message(self, fmt, *args) -> None:  # 静默默认访问日志
        pass

    # ------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        try:
            if path == "/" or path == "/index.html":
                return self._file(config.STATIC_DIR / "index.html", "text/html; charset=utf-8")
            if path.startswith("/static/"):
                return self._file(config.STATIC_DIR / Path(path[len("/static/"):]).name,
                                  self._guess_type(path))
            if path.startswith("/evidence/"):
                name = Path(path).name
                if not name.endswith(".svg"):
                    raise ApiError(404, "证据文件不存在")
                return self._file(config.EVIDENCE_DIR / name, "image/svg+xml")
            if not path.startswith("/api/"):
                raise ApiError(404, "页面不存在")
            self._handle_api_get(path, qs)
        except ApiError as e:
            self._json({"error": e.message}, e.status)
        except (ServiceError, NotFound) as e:
            self._json({"error": str(e)}, 400)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if not path.startswith("/api/"):
                raise ApiError(404, "接口不存在")
            self._handle_api_post(path)
        except ApiError as e:
            self._json({"error": e.message}, e.status)
        except auth.PermissionError_ as e:
            self._json({"error": str(e)}, 403)
        except (ServiceError, NotFound) as e:
            self._json({"error": str(e)}, 400)

    @staticmethod
    def _guess_type(path: str) -> str:
        if path.endswith(".css"):
            return "text/css; charset=utf-8"
        if path.endswith(".js"):
            return "application/javascript; charset=utf-8"
        return "application/octet-stream"

    # ------------------------------------------------------------ GET API
    def _handle_api_get(self, path: str, qs: dict) -> None:
        svc, conn = _service()
        try:
            if path == "/api/constants":
                return self._json({
                    "anomaly_labels": [
                        {"code": k, "short": v[0], "full": v[1]}
                        for k, v in ANOMALY_LABELS.items()],
                    "release_reasons": [{"code": k, "full": v}
                                        for k, v in RELEASE_REASONS.items()],
                    "role_names": auth.ROLE_NAMES,
                })
            operator = self._current_operator()
            if path == "/api/me":
                return self._json({"login": operator.login, "display_name": operator.display_name,
                                   "role": operator.role,
                                   "role_name": auth.ROLE_NAMES[operator.role],
                                   "permissions": sorted(
                                       a for a, roles in auth.PERMISSIONS.items()
                                       if operator.role in roles)})
            if path == "/api/stats":
                return self._json(queries.stats(conn))
            if path == "/api/lots":
                return self._json(queries.lots(conn))
            if path == "/api/lanes":
                return self._json({"lanes": queries.lanes(conn),
                                   "lots": queries.lots(conn)})
            if path == "/api/lane-messages":
                lane_id = int(qs.get("lane_id", ["0"])[0])
                return self._json(queries.lane_messages(conn, lane_id))
            if path == "/api/sessions":
                return self._json(queries.sessions(conn, qs.get("status", [None])[0]))
            if path.startswith("/api/sessions/"):
                sid = int(path.rsplit("/", 1)[-1])
                detail = queries.session_detail(conn, sid)
                if not detail:
                    raise ApiError(404, "停车会话不存在")
                return self._json(detail)
            if path == "/api/cases":
                return self._json(queries.cases(conn, qs.get("status", [None])[0]))
            if path == "/api/rules":
                return self._json(queries.rules(conn))
            if path == "/api/releases":
                return self._json(queries.releases(conn))
            if path == "/api/refunds":
                return self._json(queries.refunds(conn))
            if path == "/api/evidence":
                files = sorted(config.EVIDENCE_DIR.glob("*.svg"), reverse=True)
                return self._json([{"file": f.name} for f in files])
            raise ApiError(404, "接口不存在")
        finally:
            conn.close()

    # ------------------------------------------------------------ POST API
    def _handle_api_post(self, path: str) -> None:
        body = self._body()
        svc, conn = _service()
        try:
            if path == "/api/login":
                op = auth.authenticate(conn, body.get("login", ""), body.get("pass_code", ""))
                if op is None:
                    raise ApiError(401, "工号或口令不正确（虚构演示环境）")
                token = secrets.token_hex(16)
                TOKENS[token] = op
                return self._json({"token": token, "display_name": op.display_name,
                                   "role": op.role, "role_name": auth.ROLE_NAMES[op.role]})

            operator = self._current_operator()

            if path == "/api/ingest":
                operator.require("message.ingest")
                result = svc.ingest_message(
                    msg_uid=body["msg_uid"], lane_code=body["lane_code"],
                    device_time=body["device_time"], direction=body["direction"],
                    raw_plate=body.get("raw_plate") or None,
                    confidence=float(body.get("confidence", 0.95)),
                    vehicle_id=body.get("vehicle_id"),
                    coupon_code=body.get("coupon_code") or None,
                    note=body.get("note", ""))
                return self._json(result)

            if path == "/api/rules":
                operator.require("rule.create")
                rid = svc.create_rule_version(
                    operator, int(body["lot_id"]),
                    free_minutes=int(body["free_minutes"]),
                    unit_minutes=int(body["unit_minutes"]),
                    unit_price_fen=int(body["unit_price_fen"]),
                    daily_cap_fen=int(body["daily_cap_fen"]),
                    valid_from=body["valid_from"], note=body.get("note", ""))
                return self._json({"id": rid})

            parts = path.strip("/").split("/")
            # /api/cases/<id>/resolve
            if len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[3] == "resolve":
                svc.resolve_case(operator, int(parts[2]),
                                 body.get("resolution", ""), body.get("verdict", "RESOLVED"))
                return self._json({"ok": True})
            # /api/sessions/<id>/release
            if len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "release":
                rid = svc.create_release(
                    operator, int(parts[2]), body["reason_code"],
                    body.get("reason_detail", ""), body["evidence_file"])
                return self._json({"id": rid})
            # /api/orders/<id>/refund
            if len(parts) == 4 and parts[:2] == ["api", "orders"] and parts[3] == "refund":
                refund, idempotent = svc.refund_order(
                    operator, int(parts[2]), body.get("reason", ""))
                return self._json({"refund": refund, "idempotent": idempotent})
            # /api/orders/<id>/retry
            if len(parts) == 4 and parts[:2] == ["api", "orders"] and parts[3] == "retry":
                return self._json(svc.retry_charge(operator, int(parts[2])))
            # /api/vehicles/<id>/bind-method：补绑有效支付方式（模拟）
            if (len(parts) == 4 and parts[:2] == ["api", "vehicles"]
                    and parts[3] == "bind-method"):
                operator.require("payment.bind")
                mid = svc.bind_payment_method(
                    int(parts[2]), body.get("method_type", "WALLET"),
                    body.get("display_name", "补绑支付方式"),
                    body.get("token", ""))
                return self._json({"id": mid})

            raise ApiError(404, "接口不存在")
        finally:
            conn.close()


def main() -> int:
    config.ensure_dirs()
    conn = connect()
    init_db(conn)
    conn.close()
    seed(reset=False)  # 首次启动自动生成样例
    httpd = ThreadingHTTPServer(("0.0.0.0", config.DEFAULT_PORT), Handler)
    print(f"商场停车无感扣费异常复核平台已启动: http://localhost:{config.DEFAULT_PORT}")
    print("演示账号: admin/admin123  fang/op123  shen/au123  qian/fi123")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
