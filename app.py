"""Run the localhost UI/API and LAN-facing TMvision receiver."""

from __future__ import annotations

import logging
import threading
from logging.handlers import RotatingFileHandler
from typing import Any

from waitress import create_server

from tmchess.config import CONFIG, SITE_ROOT
from tmchess.web import create_apps
from tmchess.robot import HardwareLease


def _create_http_server(application: Any, host: str, port: int, *, threads: int):
    """Bind a production WSGI server without starting its event loop."""
    return create_server(
        application,
        host=host,
        port=port,
        threads=threads,
        ident="TMChess",
        clear_untrusted_proxy_headers=True,
    )


def _close_http_server(server: Any) -> None:
    """Stop accepting requests and release Waitress worker threads."""
    try:
        server.close()
    finally:
        server.task_dispatcher.shutdown()


def main() -> None:
    log_root = SITE_ROOT / "系統紀錄"
    log_root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s pid=%(process)d thread=%(threadName)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                log_root / "system.log",
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            ),
        ],
        force=True,
    )
    ui_app, vision_app, service, inbox = create_apps(CONFIG, start_preflight_monitor=False)
    vision_server = None
    ui_server = None
    vision_thread = None
    try:
        # Four workers are sufficient for sequential TMvision uploads. The UI
        # gets additional workers because each open SSE stream occupies one.
        vision_server = _create_http_server(
            vision_app,
            CONFIG.network.vision_host,
            CONFIG.network.vision_port,
            threads=4,
        )
        ui_server = _create_http_server(
            ui_app,
            CONFIG.network.ui_host,
            CONFIG.network.ui_port,
            threads=8,
        )
        inbox.set_receiver_ready(True)
        vision_thread = threading.Thread(
            target=vision_server.run,
            name="tmvision-http",
            daemon=True,
        )
        vision_thread.start()
        service.start_preflight_monitor()
        print(f"UI: http://{CONFIG.network.ui_host}:{CONFIG.network.ui_port}")
        print(f"Hardware mode: {'ENABLED' if CONFIG.safety.hardware_enabled else 'DISABLED'}")
        print(
            "TMvision receiver: "
            f"{CONFIG.network.vision_host}:{CONFIG.network.vision_port}"
            f"{CONFIG.network.vision_path}"
        )
        ui_server.run()
    except KeyboardInterrupt:
        pass
    finally:
        inbox.set_receiver_ready(False)
        # Persist the stop/uncertain-motion marker before spending time waiting
        # for HTTP servers to shut down.
        try:
            service.close()
        except Exception:
            logging.getLogger(__name__).exception("Service cleanup failed")
        for server in (vision_server, ui_server):
            if server is not None:
                try:
                    _close_http_server(server)
                except Exception:
                    logging.getLogger(__name__).exception("HTTP server cleanup failed")
        if vision_thread is not None:
            vision_thread.join(timeout=5.0)
            if vision_thread.is_alive():
                logging.getLogger(__name__).warning("TMvision HTTP thread did not stop in time")


if __name__ == "__main__":
    website_lease = HardwareLease(SITE_ROOT / "runtime" / "website.lock")
    website_lease.acquire()
    try:
        main()
    finally:
        website_lease.release()
