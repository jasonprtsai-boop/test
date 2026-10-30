"""Flask applications for localhost UI/API and isolated TMvision upload."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge

from .config import CONFIG, SITE_ROOT, AppConfig
from .engine import PikafishEngine
from .records import GameRecordWriter
from .robot import TMRobotAdapter
from .service import CommandConflict, GameService, StatePersistenceError
from .session import GameSessionStore
from .vision import CAPTURE_ID_PATTERN, DualViewInbox, DualViewProcessor, VisionAdapter


def build_runtime(config: AppConfig = CONFIG, *, start_preflight_monitor: bool = True):
    inbox = DualViewInbox(
        config.capture_root,
        config.network.robot_ip,
        config.network.max_upload_bytes,
        # TMflow executes the two External Image Processing nodes serially and
        # waits for the first HTTP response before sending the second frame.
        # The deployed cell currently uploads both views at 2592x1944, so view
        # identity must follow that guaranteed graph order instead of image size.
    )
    processor = DualViewProcessor(config.vision)
    vision = VisionAdapter(inbox, processor, config.vision.image_timeout_seconds)
    robot = TMRobotAdapter(config.network, config.motion)
    engine = PikafishEngine(config.engine)
    service = GameService(
        config,
        robot,
        vision,
        engine,
        start_preflight_monitor=start_preflight_monitor,
        session_store=GameSessionStore(config.capture_root),
        record_writer=GameRecordWriter(config.record_root, games_root=config.capture_root),
    )
    return service, inbox


def create_ui_app(
    service: GameService,
    config: AppConfig = CONFIG,
) -> Flask:
    app = Flask(
        "tmchess-ui",
        template_folder=str(SITE_ROOT / "templates"),
        static_folder=str(SITE_ROOT / "static"),
    )
    app.config.update(
        JSON_AS_ASCII=False,
        TRUSTED_HOSTS=["127.0.0.1", "localhost"],
    )

    protected_post_prefixes = ("/api/game/", "/api/operator/")
    # Leave at least four of the eight UI workers available for controls.
    event_slots = threading.BoundedSemaphore(4)

    def origin_allowed(origin: str | None) -> bool:
        if not origin:
            return False
        parsed = urlsplit(origin)
        try:
            origin_port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"}
            and origin_port == config.network.ui_port
        )

    @app.before_request
    def protect_control_commands():
        if request.method != "POST" or not request.path.startswith(protected_post_prefixes):
            return None
        if not request.is_json:
            return jsonify({
                "error": {
                    "code": "json_required",
                    "message": "控制命令只接受 application/json。",
                }
            }), 415
        if not origin_allowed(request.headers.get("Origin")):
            return jsonify({
                "error": {
                    "code": "origin_not_allowed",
                    "message": "控制命令必須來自本機網站。",
                }
            }), 403
        return None

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'"
        )
        return response

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/player")
    def player():
        return render_template("player.html")

    @app.get("/monitor")
    def monitor():
        return render_template("monitor.html")

    @app.get("/api/state")
    def state():
        return jsonify(service.get_state())

    @app.post("/api/engine/depth")
    def engine_depth():
        body = request.get_json(silent=True)
        try:
            return jsonify(service.set_engine_depth(body.get("depth") if isinstance(body, dict) else None))
        except ValueError as exc:
            return jsonify({"error": {"code": "invalid_depth", "message": str(exc)}}), 400

    @app.post("/api/engine/movetime")
    def engine_movetime():
        body = request.get_json(silent=True)
        try:
            return jsonify(service.set_engine_movetime(body.get("movetime_ms") if isinstance(body, dict) else None))
        except ValueError as exc:
            return jsonify({"error": {"code": "invalid_movetime", "message": str(exc)}}), 400

    @app.post("/api/game/reset-page")
    def reset_page():
        return jsonify(service.submit_reset_page())

    @app.get("/api/events")
    def events():
        if not event_slots.acquire(blocking=False):
            return jsonify({"error": {"code": "event_capacity", "message": "即時連線已滿，請使用狀態查詢。"}}), 503
        released = False
        release_lock = threading.Lock()

        def release_slot():
            nonlocal released
            with release_lock:
                if not released:
                    released = True
                    event_slots.release()

        try:
            last_revision = int(request.headers.get("Last-Event-ID", request.args.get("revision", "-1")))
        except ValueError:
            last_revision = -1

        def messages():
            revision = last_revision
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                current = service.get_state()
                if current["revision"] > revision:
                    revision = current["revision"]
                    yield f"id: {revision}\nevent: state\ndata: {json.dumps(current, ensure_ascii=False)}\n\n"
                else:
                    changed = service.wait_state_after(revision, timeout=min(15, max(0, deadline - time.monotonic())))
                    if changed is None:
                        yield ": heartbeat\n\n"
                    else:
                        revision = changed["revision"]
                        yield f"id: {revision}\nevent: state\ndata: {json.dumps(changed, ensure_ascii=False)}\n\n"

        def stream():
            try:
                yield from messages()
            finally:
                release_slot()

        response = Response(stream(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        })
        response.call_on_close(release_slot)
        return response

    def body() -> dict[str, Any]:
        value = request.get_json(silent=True)
        return value if isinstance(value, dict) else {}

    @app.post("/api/game/start")
    def game_start():
        return jsonify(service.submit_start(str(body().get("first_actor", "")))), 202

    @app.post("/api/game/start-mode")
    def game_start_mode():
        value = body()
        return jsonify(service.submit_start_mode(str(value.get("mode", "")), record_name=str(value.get("record_name", "")))), 202

    @app.post("/api/game/start-current")
    def game_start_current():
        value = body()
        return jsonify(service.submit_start_current(
            str(value.get("player_color", "red")),
            str(value.get("current_actor", "")),
            mode=str(value["mode"]) if "mode" in value else None,
            continuation=str(value.get("continuation", "auto")),
            record_name=str(value.get("record_name", "")),
        )), 202

    @app.post("/api/game/player-done")
    def player_done():
        return jsonify(service.submit_player_done()), 202

    @app.post("/api/game/hint")
    def game_hint():
        return jsonify(service.submit_hint()), 202

    @app.post("/api/game/execute-engine")
    def game_execute_engine():
        return jsonify(service.submit_execute_engine_move()), 202

    @app.post("/api/game/resume")
    def game_resume():
        return jsonify(service.submit_resume(str(body().get("current_actor", "")))), 202

    @app.post("/api/game/retry")
    def retry():
        return jsonify(service.submit_retry()), 202

    @app.post("/api/game/stop")
    def stop():
        return jsonify(service.submit_stop()), 202

    @app.get("/api/game/record")
    def game_record():
        path = service.current_record_path()
        if path is None:
            return jsonify({
                "error": {
                    "code": "record_not_ready",
                    "message": "目前沒有可下載的對局 Excel，請稍後再試。",
                }
            }), 404
        return send_file(
            path,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=path.name,
            conditional=True,
        )

    @app.post("/api/operator/confirm-base-tcp")
    def confirm_base_tcp():
        value = body()
        return jsonify(service.submit_confirm_base_tcp(
            str(value.get("base", "")),
            str(value.get("tcp", "")),
        )), 202

    @app.post("/api/operator/confirm-safe-stop")
    def confirm_safe_stop():
        return jsonify(service.submit_confirm_safe_stop(body().get("confirmed") is True)), 202

    @app.get("/api/media/<capture_id>/<view>")
    def media(capture_id: str, view: str):
        if not CAPTURE_ID_PATTERN.fullmatch(capture_id) or view not in ("v1", "v2"):
            return jsonify({"error": {"code": "invalid_media_path", "message": "影像路徑無效。"}}), 404
        filename = f"{view}-yolo.jpg"
        root = config.capture_root.resolve()
        game_id = service.get_state().get("game", {}).get("id")
        if game_id and CAPTURE_ID_PATTERN.fullmatch(str(game_id)):
            game_directory = (root / str(game_id)).resolve()
            matches = list(game_directory.glob(f"*_{capture_id}_{view}_yolo.jpg"))
            path = matches[0].resolve() if len(matches) == 1 else Path()
            if len(matches) != 1:
                # Read-only compatibility for sessions created before the
                # single-folder naming scheme.
                path = (game_directory / "captures" / capture_id / filename).resolve()
        else:
            # Legacy flat capture folders remain readable until a game is active.
            path = (root / capture_id / filename).resolve()
        if root not in path.parents or not path.is_file():
            return jsonify({"error": {"code": "media_not_found", "message": "找不到影像。"}}), 404
        response = send_file(path, mimetype="image/jpeg", conditional=True)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(CommandConflict)
    def conflict(exc: CommandConflict):
        return jsonify({"error": {"code": exc.code, "message": str(exc)}, "state": service.get_state()}), 409

    @app.errorhandler(StatePersistenceError)
    def persistence_unavailable(exc: StatePersistenceError):
        return jsonify({
            "error": {"code": exc.code, "message": str(exc)},
            "state": service.get_state(),
        }), 503

    return app


def create_vision_app(inbox: DualViewInbox, config: AppConfig = CONFIG) -> Flask:
    app = Flask("tmchess-vision")
    app.config["MAX_CONTENT_LENGTH"] = config.network.max_upload_bytes

    @app.post(config.network.vision_path)
    def receive_image():
        if request.remote_addr != config.network.robot_ip:
            return jsonify({"error": "source_ip_not_allowed"}), 403
        uploaded = request.files.get("file") or request.files.get("image")
        if uploaded is None:
            return jsonify({"error": "jpeg_multipart_required"}), 400
        data = uploaded.stream.read(config.network.max_upload_bytes + 1)
        try:
            _capture_id, _view, (image_w, image_h) = inbox.accept(
                request.remote_addr, uploaded.mimetype or "", data
            )
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        # TMflow 1.82.51 External Image Processing advances only after it can
        # parse the documented detection result shape. The website performs the
        # real YOLO fusion after both views arrive; this acknowledgement marker
        # exists only to let the TMflow graph continue to the next viewpoint.
        return jsonify({
            "message": "success",
            "image_w": image_w,
            "image_h": image_h,
            "annotations": [{
                "label": "capture_ok",
                "score": 0.99,
                "box_cx": image_w / 2,
                "box_cy": image_h / 2,
                "box_w": 100,
                "box_h": 100,
                "rotation": 0.0,
            }],
        })

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_exc):
        return jsonify({"error": "upload_too_large"}), 413

    @app.get("/")
    def no_public_index():
        return jsonify({"error": "not_found"}), 404

    return app


def create_apps(
    config: AppConfig = CONFIG,
    service: GameService | None = None,
    inbox: DualViewInbox | None = None,
    *,
    start_preflight_monitor: bool = True,
):
    if service is None or inbox is None:
        service, inbox = build_runtime(config, start_preflight_monitor=start_preflight_monitor)
    return create_ui_app(service, config), create_vision_app(inbox, config), service, inbox
