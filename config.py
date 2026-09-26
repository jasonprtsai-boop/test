from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
YOLO_MODEL_PATH = PROJECT_DIR / "models" / "best.pt"
YOLO_CONFIDENCE = 0.5

ROBOT_IP = "192.168.1.2"
LISTEN_PORT = 5890
SERVER_PORT = 6189
SIMULATION_MODE = True
# Four image-space intersections in TL, TR, BR, BL order. Measure these from
# the TM camera image before enabling physical mode.
CAMERA_CORNERS = None

Z_SAFE = 180.0
Z_PICK = 95.0
Z_APPROACH = 115.0
# Open-loop timing fallback for TMflow motion commands. Measure the slowest
# safe motion and add margin before using physical mode.
MOTION_SETTLE_SECONDS = 1.2
VACUUM_SETTLE_SECONDS = 0.4
RELEASE_SETTLE_SECONDS = 0.2

GRIPPER_DO_DEVICE = "ControlBox"  # "ControlBox" or "EndModule"
GRIPPER_DO_PIN = 3

CORNER_COORDS = {
    "BL": (250.0, -150.0),
    "BR": (250.0, 150.0),
    "TL": (600.0, -150.0),
    "TR": (600.0, 150.0),
}
GRAVEYARD_DROP_POS = (200.0, 200.0)
GRAVEYARD_START = GRAVEYARD_DROP_POS  # alias for backward compatibility

ENGINE_PATH = PROJECT_DIR / "engine_bin" / "pikafish.exe"
ENGINE_NNUE_PATH = PROJECT_DIR / "engine_bin" / "pikafish.nnue"
ENGINE_TIME_LIMIT_MS = 500
ENGINE_TIMEOUT_SECONDS = 15.0
CAMERA_IMAGE_TIMEOUT = 10.0
