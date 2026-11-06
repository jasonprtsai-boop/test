"""Standalone TMvision setup receiver. Never opens a robot connection."""
from datetime import datetime
from uuid import uuid4

from flask import Flask, jsonify, request
from waitress import serve

from tmchess.board import PIECE_NAMES
from tmchess.config import CONFIG, SITE_ROOT
from tmchess.vision import DualViewInbox, DualViewProcessor

PHOTO_ROOT = SITE_ROOT / "runtime" / "tmvision-preview"
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = CONFIG.network.max_upload_bytes
processor = DualViewProcessor(CONFIG.vision)


@app.get("/")
def status():
    return jsonify(service="TMvision preview", photos=str(PHOTO_ROOT))


@app.route(CONFIG.network.vision_path, methods=["GET", "POST"])
@app.route(CONFIG.network.vision_path + "/<view>", methods=["GET", "POST"])
def detect(view="preview"):
    if request.remote_addr not in {"127.0.0.1", CONFIG.network.robot_ip}:
        return jsonify(error="source_ip_not_allowed"), 403
    if view not in {"preview", "v1", "v2"}:
        return jsonify(error="unknown_view"), 404
    if request.method == "GET":
        return jsonify(message="TMvision endpoint ready")
    uploaded = request.files.get("file")
    if uploaded is None:
        uploaded = request.files.get("image")
    if uploaded is None:
        return jsonify(error="jpeg_multipart_required"), 400
    data = uploaded.read()
    try:
        width, height = DualViewInbox._jpeg_size(data)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    now = datetime.now()
    folder = PHOTO_ROOT / now.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{now:%H%M%S_%f}_{view}_{uuid4().hex[:8]}"
    raw = folder / f"{stem}.jpg"
    raw.write_bytes(data)
    boxes, _ = processor._detect(raw, folder / f"{stem}_yolo.jpg")
    annotations = [
        dict(label=PIECE_NAMES[b["piece"] - 1], score=b["confidence"],
             box_cx=b["cx"], box_cy=b["cy"], box_w=b["width"],
             box_h=b["height"], rotation=0.0)
        for b in boxes
    ]
    print(f"[{view}] {len(boxes)} detections -> {raw} | {stem}_yolo.jpg", flush=True)
    return jsonify(message="success", annotations=annotations, image_w=width, image_h=height)


if __name__ == "__main__":
    try:
        processor.check_ready()
        print(f"TMvision receiver on port {CONFIG.network.vision_port}. Photos: {PHOTO_ROOT}", flush=True)
        serve(app, host=CONFIG.network.vision_host, port=CONFIG.network.vision_port, threads=2)
    finally:
        processor.close()
