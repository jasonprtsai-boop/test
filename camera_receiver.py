"""Small HTTP receiver for TMflow External Detection image posts."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock, Thread
from typing import Optional

import cv2
import numpy as np


class CameraReceiver:
    def __init__(self, host: str = "0.0.0.0", port: int = 6189) -> None:
        self._image: Optional[np.ndarray] = None
        self._lock = Lock()
        self._event = Event()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"message":"Python Server Ready"}')

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = self.rfile.read(length)
                    image = cv2.imdecode(
                        np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR
                    )
                    if image is None:
                        raise ValueError("invalid image payload")
                    with receiver._lock:
                        receiver._image = image
                    receiver._event.set()
                except (ValueError, TypeError):
                    self.send_error(400, "invalid image payload")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"message":"success"}')

            def log_message(self, format, *args):
                return

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.port = self.server.server_address[1]
        self._thread: Optional[Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def clear(self) -> None:
        with self._lock:
            self._image = None
            self._event.clear()

    def wait_for_image(self, timeout: float = 6.0) -> np.ndarray:
        if not self._event.wait(timeout):
            raise TimeoutError("camera image timed out")
        with self._lock:
            if self._image is None:
                raise RuntimeError("camera image is unavailable")
            image = self._image.copy()
            self._image = None
            self._event.clear()
        return image