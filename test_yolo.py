from ultralytics import YOLO
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import math


# ============================================================
# 1. 基本設定
# ============================================================

BASE_DIR = Path(
    r"C:\Users\user\Desktop\專題\專題程式\test"
)

MODEL_PATH = BASE_DIR / "best.pt"


# ============================================================
# 2. Cycle
# ============================================================

CYCLE = 1
CYCLE_TEXT = f"{CYCLE:03d}"


# ============================================================
# 3. YOLO
# ============================================================

CONF_THRESHOLD = 0.25


# ============================================================
# 4. 中國象棋棋盤
# ============================================================

BOARD_ROWS = 10
BOARD_COLS = 9

CURRENT_TURN = "red"


# ============================================================
# 5. FEN Mapping
# ============================================================

LABEL_TO_FEN = {

    # 黑方
    "chess-black-king": "k",
    "chess-black-guard": "a",
    "chess-black-elephant": "b",
    "chess-black-horse": "n",
    "chess-black-car": "r",
    "chess-black-cannon": "c",
    "chess-black-soldier": "p",

    # 紅方
    "chess-red-king": "K",
    "chess-red-guard": "A",
    "chess-red-elephant": "B",
    "chess-red-horse": "N",
    "chess-red-car": "R",
    "chess-red-cannon": "C",
    "chess-red-soldier": "P",
}


# ============================================================
# 6. V1 人工90點校正
#
# 你人工畫線的校正圖片：
# 2048 × 1536
#
# 實際正式 V1：
# 2592 × 1944
#
# 所以 V1 需要自動 Scale。
# ============================================================

V1_CALIBRATION_WIDTH = 2048
V1_CALIBRATION_HEIGHT = 1536


V1_CALIBRATION_COL_X = {

    0: 807.5,
    1: 944.0,
    2: 1081.0,
    3: 1218.0,
    4: 1355.5,
    5: 1492.0,
    6: 1629.0,
    7: 1764.5,
    8: 1901.0,
}


V1_CALIBRATION_ROW_Y = {

    # 上半
    0: 155.0,
    1: 295.5,
    2: 433.5,
    3: 571.5,
    4: 709.5,

    # 楚河漢界

    # 下半
    5: 832.0,
    6: 969.0,
    7: 1104.0,
    8: 1242.0,
    9: 1380.0,
}


V1_MAX_GRID_DISTANCE = 105


# ============================================================
# 7. V2 校正
#
# ★★★ 重要修正 ★★★
#
# 這組 V2 座標本身就是從：
#
# 2592 × 1944
#
# 的正式 V2 圖片得到。
#
# 所以 Calibration Size 必須也是：
#
# 2592 × 1944
#
# 不可以再設成 2048 × 1536。
# ============================================================

V2_CALIBRATION_WIDTH = 2592
V2_CALIBRATION_HEIGHT = 1944


# ============================================================
# V2 Column
#
# 根據目前實際 Detection：
#
# C0 約 1265~1279
# C1 約 1509~1513
# C2 約 1754~1781
# C3 約 2025
# ============================================================

V2_CALIBRATION_COL_X = {
    0: 1252.0,   # C0 再往左
    1: 1506.0,   # C1 再往左
    2: 1765.0,   # C2 再往左
    3: 2025.0,   # C3 先不動
}

V2_CALIBRATION_ROW_Y = {
    0: 929.0,
    1: 1189.0,   # R1 再往上
    2: 1430.0,
    3: 1690.0,
}

# ============================================================
# V2 判斷容許值
#
# 使用 dx/dy 分開判斷。
# ============================================================

V2_MAX_DX = 125
V2_MAX_DY = 125


# ============================================================
# 8. V2 → Main Board
#
# V2 完整接管：
#
# Main R1~R4
# Main C0~C3
#
# 16格
# ============================================================

V2_TO_MAIN_BOARD = {

    # V2 R0 → Main R1
    (0, 0): (1, 0),
    (0, 1): (1, 1),
    (0, 2): (1, 2),
    (0, 3): (1, 3),

    # V2 R1 → Main R2
    (1, 0): (2, 0),
    (1, 1): (2, 1),
    (1, 2): (2, 2),
    (1, 3): (2, 3),

    # V2 R2 → Main R3
    (2, 0): (3, 0),
    (2, 1): (3, 1),
    (2, 2): (3, 2),
    (2, 3): (3, 3),

    # V2 R3 → Main R4
    (3, 0): (4, 0),
    (3, 1): (4, 1),
    (3, 2): (4, 2),
    (3, 3): (4, 3),
}


V2_MAIN_CELLS = set(
    V2_TO_MAIN_BOARD.values()
)


# ============================================================
# 9. 找最新同組 V1 / V2
# ============================================================

def find_latest_v1_v2_pair():

    candidate_folders = []


    for v1_path in BASE_DIR.rglob(
        f"TM_C{CYCLE_TEXT}_V1_*.jpg"
    ):

        folder = v1_path.parent

        v2_files = list(
            folder.glob(
                f"TM_C{CYCLE_TEXT}_V2_*.jpg"
            )
        )

        if v2_files:

            candidate_folders.append(
                folder
            )


    if not candidate_folders:

        raise FileNotFoundError(
            f"找不到 Cycle {CYCLE} "
            f"同資料夾的 V1 / V2"
        )


    candidate_folders = list(
        set(candidate_folders)
    )


    latest_folder = max(

        candidate_folders,

        key=lambda folder:

            max(

                f.stat().st_mtime

                for f in folder.glob(
                    f"TM_C{CYCLE_TEXT}_V*.jpg"
                )

            )

    )


    v1_files = list(
        latest_folder.glob(
            f"TM_C{CYCLE_TEXT}_V1_*.jpg"
        )
    )


    v2_files = list(
        latest_folder.glob(
            f"TM_C{CYCLE_TEXT}_V2_*.jpg"
        )
    )


    v1_image = max(
        v1_files,
        key=lambda p: p.stat().st_mtime
    )


    v2_image = max(
        v2_files,
        key=lambda p: p.stat().st_mtime
    )


    return (
        latest_folder,
        v1_image,
        v2_image
    )


# ============================================================
# 10. Empty Board
# ============================================================

def create_empty_board():

    return [

        [
            None
            for _ in range(
                BOARD_COLS
            )
        ]

        for _ in range(
            BOARD_ROWS
        )

    ]


# ============================================================
# 11. Scale X
# ============================================================

def scale_x_coordinates(
    calibration,
    calibration_width,
    actual_width
):

    scale = (

        actual_width
        /
        calibration_width

    )


    return {

        index:
            value * scale

        for index, value
        in calibration.items()

    }


# ============================================================
# 12. Scale Y
# ============================================================

def scale_y_coordinates(
    calibration,
    calibration_height,
    actual_height
):

    scale = (

        actual_height
        /
        calibration_height

    )


    return {

        index:
            value * scale

        for index, value
        in calibration.items()

    }


# ============================================================
# 13. Build V1 90 Grid
# ============================================================

def build_v1_manual_grid(
    image_width,
    image_height
):

    col_x = scale_x_coordinates(

        V1_CALIBRATION_COL_X,

        V1_CALIBRATION_WIDTH,

        image_width

    )


    row_y = scale_y_coordinates(

        V1_CALIBRATION_ROW_Y,

        V1_CALIBRATION_HEIGHT,

        image_height

    )


    grid = {}


    for row in range(
        BOARD_ROWS
    ):

        for col in range(
            BOARD_COLS
        ):

            grid[
                (row, col)
            ] = (

                float(
                    col_x[col]
                ),

                float(
                    row_y[row]
                )

            )


    return (
        grid,
        col_x,
        row_y
    )


# ============================================================
# 14. Build V2 16 Grid
#
# 因 Calibration Size 已修正為2592×1944，
# 如果正式圖片也是2592×1944，
# Scale = 1.0。
# ============================================================

def build_v2_grid(
    image_width,
    image_height
):

    col_x = scale_x_coordinates(

        V2_CALIBRATION_COL_X,

        V2_CALIBRATION_WIDTH,

        image_width

    )


    row_y = scale_y_coordinates(

        V2_CALIBRATION_ROW_Y,

        V2_CALIBRATION_HEIGHT,

        image_height

    )


    grid = {}


    for row in range(
        4
    ):

        for col in range(
            4
        ):

            grid[
                (row, col)
            ] = (

                float(
                    col_x[col]
                ),

                float(
                    row_y[row]
                )

            )


    return (
        grid,
        col_x,
        row_y
    )


# ============================================================
# 15. YOLO
# ============================================================

def run_yolo(
    model,
    image_path,
    output_path,
    view_name
):

    print()

    print("=" * 110)

    print(
        f"{view_name} YOLO"
    )

    print(
        image_path
    )

    print("=" * 110)


    results = model.predict(

        source=str(
            image_path
        ),

        conf=CONF_THRESHOLD,

        verbose=False

    )


    result = results[0]


    result.save(
        filename=str(
            output_path
        )
    )


    detections = []


    if (
        result.boxes is None
        or
        len(result.boxes) == 0
    ):

        return detections


    for i, box in enumerate(
        result.boxes
    ):

        cls_id = int(
            box.cls[0]
        )


        label = (
            model.names[
                cls_id
            ]
        )


        confidence = float(
            box.conf[0]
        )


        x1, y1, x2, y2 = (

            box.xyxy[0]

            .cpu()

            .numpy()

            .tolist()

        )


        cx = (
            x1 + x2
        ) / 2


        cy = (
            y1 + y2
        ) / 2


        detection = {

            "index": i,

            "label":
                label,

            "confidence":
                confidence,

            "x1":
                x1,

            "y1":
                y1,

            "x2":
                x2,

            "y2":
                y2,

            "cx":
                cx,

            "cy":
                cy,

        }


        detections.append(
            detection
        )


        print(

            f"[{i:02d}] "

            f"{label:<25} "

            f"conf="
            f"{confidence:.4f} "

            f"center=("
            f"{cx:.1f}, "
            f"{cy:.1f}"
            f")"

        )


    return detections


# ============================================================
# 16. 最近 Grid
# ============================================================

def find_nearest_grid_cell(
    cx,
    cy,
    grid
):

    best_cell = None

    best_distance = float(
        "inf"
    )


    for cell, (
        gx,
        gy
    ) in grid.items():

        distance = math.hypot(

            cx - gx,

            cy - gy

        )


        if (
            distance
            <
            best_distance
        ):

            best_distance = (
                distance
            )

            best_cell = (
                cell
            )


    return (
        best_cell,
        best_distance
    )


# ============================================================
# 17. V1 → Board
# ============================================================

def build_v1_board(
    detections,
    grid
):

    board = (
        create_empty_board()
    )


    info = {}


    skipped_v2 = []

    ignored = []


    print()

    print("=" * 110)

    print(
        "V1 → MANUAL 90 GRID"
    )

    print("=" * 110)


    for det in detections:

        (
            cell,
            distance
        ) = find_nearest_grid_cell(

            det["cx"],

            det["cy"],

            grid

        )


        row, col = (
            cell
        )


        if (
            distance
            >
            V1_MAX_GRID_DISTANCE
        ):

            ignored.append(
                det
            )


            print(

                f"[V1 IGNORE] "

                f"{det['label']:<25} "

                f"Nearest=R{row}C{col} "

                f"d={distance:.1f}"

            )


            continue


        # ====================================================
        # V2 接管
        # ====================================================

        if (
            cell
            in
            V2_MAIN_CELLS
        ):

            skipped_v2.append(
                det
            )


            print(

                f"[V1 → V2] "

                f"{det['label']:<25} "

                f"R{row}C{col} "

                f"d={distance:.1f}"

            )


            continue


        old = (
            info.get(
                cell
            )
        )


        if (
            old is not None
            and
            old["confidence"]
            >=
            det["confidence"]
        ):

            continue


        board[
            row
        ][
            col
        ] = (
            det[
                "label"
            ]
        )


        info[
            cell
        ] = {

            **det,

            "row":
                row,

            "col":
                col,

            "distance":
                distance,

            "source":
                "V1",

        }


        print(

            f"{det['label']:<25} "

            f"→ R{row}C{col} "

            f"d="
            f"{distance:.1f}"

        )


    return (
        board,
        info,
        skipped_v2,
        ignored
    )


# ============================================================
# 18. V2 → Patch
# ============================================================

def build_v2_patch(
    detections,
    grid
):

    patch = [

        [
            None
            for _ in range(4)
        ]

        for _ in range(4)

    ]


    info = {}

    ignored = []


    print()

    print("=" * 110)

    print(
        "V2 → AUTHORITATIVE 4×4"
    )

    print("=" * 110)


    for det in detections:

        (
            local_cell,
            distance
        ) = find_nearest_grid_cell(

            det["cx"],

            det["cy"],

            grid

        )


        local_row, local_col = (
            local_cell
        )


        gx, gy = (
            grid[
                local_cell
            ]
        )


        dx = abs(
            det["cx"] - gx
        )


        dy = abs(
            det["cy"] - gy
        )


        # ====================================================
        # 必須同時符合 X / Y
        # ====================================================

        if (
            dx > V2_MAX_DX
            or
            dy > V2_MAX_DY
        ):

            ignored.append(
                det
            )


            print(

                f"[V2 IGNORE] "

                f"{det['label']:<25} "

                f"Nearest="
                f"R{local_row}C{local_col} "

                f"dx={dx:.1f} "

                f"dy={dy:.1f}"

            )


            continue


        main_row, main_col = (

            V2_TO_MAIN_BOARD[
                local_cell
            ]

        )


        old = (
            info.get(
                local_cell
            )
        )


        if (
            old is not None
            and
            old["confidence"]
            >=
            det["confidence"]
        ):

            continue


        patch[
            local_row
        ][
            local_col
        ] = (
            det[
                "label"
            ]
        )


        info[
            local_cell
        ] = {

            **det,

            "local_row":
                local_row,

            "local_col":
                local_col,

            "main_row":
                main_row,

            "main_col":
                main_col,

            "distance":
                distance,

            "dx":
                dx,

            "dy":
                dy,

            "source":
                "V2_AUTHORITATIVE",

        }


        print(

            f"{det['label']:<25} "

            f"→ V2 "
            f"R{local_row}C{local_col} "

            f"→ Main "
            f"R{main_row}C{main_col} "

            f"dx="
            f"{dx:.1f} "

            f"dy="
            f"{dy:.1f}"

        )


    return (
        patch,
        info,
        ignored
    )


# ============================================================
# 19. Final Board
# ============================================================

def build_final_board(
    board_v1,
    info_v1,
    info_v2
):

    final_board = [

        row.copy()

        for row in board_v1

    ]


    final_info = (
        info_v1.copy()
    )


    # ========================================================
    # V2 16格全部清空
    # ========================================================

    for (
        row,
        col
    ) in V2_MAIN_CELLS:

        final_board[
            row
        ][
            col
        ] = None


        final_info.pop(
            (row, col),
            None
        )


    # ========================================================
    # 填入 V2
    # ========================================================

    for local_cell, det in (
        info_v2.items()
    ):

        row = (
            det["main_row"]
        )

        col = (
            det["main_col"]
        )


        final_board[
            row
        ][
            col
        ] = (
            det["label"]
        )


        final_info[
            (row, col)
        ] = {

            **det,

            "source":
                "V2_AUTHORITATIVE",

        }


    return (
        final_board,
        final_info
    )


# ============================================================
# 20. Font
# ============================================================

def get_font(
    size
):

    candidates = [

        r"C:\Windows\Fonts\arial.ttf",

        r"C:\Windows\Fonts\calibri.ttf",

        r"C:\Windows\Fonts\msjh.ttc",

    ]


    for path in candidates:

        if Path(path).exists():

            try:

                return ImageFont.truetype(
                    path,
                    size
                )

            except Exception:

                pass


    return ImageFont.load_default()


# ============================================================
# 21. Draw V1
# ============================================================

def draw_v1_grid(
    image_path,
    detections,
    grid,
    output_path
):

    image = Image.open(
        image_path
    ).convert(
        "RGB"
    )


    draw = ImageDraw.Draw(
        image
    )


    font_grid = get_font(16)

    font_det = get_font(18)


    # ========================================================
    # Horizontal
    # ========================================================

    for row in range(
        BOARD_ROWS
    ):

        points = [

            grid[
                (row, col)
            ]

            for col in range(
                BOARD_COLS
            )

        ]


        draw.line(

            [
                (
                    int(x),
                    int(y)
                )

                for x, y
                in points
            ],

            fill="white",

            width=2

        )


    # ========================================================
    # Vertical
    # ========================================================

    for col in range(
        BOARD_COLS
    ):

        top_points = [

            grid[
                (row, col)
            ]

            for row in range(
                0,
                5
            )

        ]


        bottom_points = [

            grid[
                (row, col)
            ]

            for row in range(
                5,
                10
            )

        ]


        draw.line(

            [
                (
                    int(x),
                    int(y)
                )

                for x, y
                in top_points
            ],

            fill="white",

            width=2

        )


        draw.line(

            [
                (
                    int(x),
                    int(y)
                )

                for x, y
                in bottom_points
            ],

            fill="white",

            width=2

        )


    # ========================================================
    # Points
    # ========================================================

    for (
        row,
        col
    ), (
        x,
        y
    ) in grid.items():

        x = int(x)

        y = int(y)


        if (
            (row, col)
            in
            V2_MAIN_CELLS
        ):

            color = "cyan"

            text = (
                f"R{row}C{col} V2"
            )

        else:

            color = "white"

            text = (
                f"R{row}C{col}"
            )


        draw.ellipse(

            (
                x - 5,
                y - 5,
                x + 5,
                y + 5
            ),

            fill=color,

            outline="black"

        )


        draw.text(

            (
                x + 5,
                y + 5
            ),

            text,

            fill=color,

            font=font_grid,

            stroke_width=2,

            stroke_fill="black"

        )


    # ========================================================
    # Detection
    # ========================================================

    for det in detections:

        (
            cell,
            distance
        ) = find_nearest_grid_cell(

            det["cx"],

            det["cy"],

            grid

        )


        row, col = cell


        cx = int(
            det["cx"]
        )

        cy = int(
            det["cy"]
        )


        short = (
            det["label"]
            .replace(
                "chess-",
                ""
            )
        )


        if (
            distance
            <=
            V1_MAX_GRID_DISTANCE
        ):

            color = "yellow"

        else:

            color = "red"


        if (
            cell
            in
            V2_MAIN_CELLS
        ):

            target = "V2"

        else:

            target = (
                f"R{row}C{col}"
            )


        draw.ellipse(

            (
                cx - 7,
                cy - 7,
                cx + 7,
                cy + 7
            ),

            fill=color,

            outline="black"

        )


        draw.text(

            (
                cx + 8,
                cy - 20
            ),

            (
                f"{short} "

                f"→ {target} "

                f"d={distance:.1f}"
            ),

            fill=color,

            font=font_det,

            stroke_width=2,

            stroke_fill="black"

        )


    image.save(
        output_path
    )


# ============================================================
# 22. Draw V2
# ============================================================

def draw_v2_grid(
    image_path,
    detections,
    grid,
    output_path
):

    image = Image.open(
        image_path
    ).convert(
        "RGB"
    )


    draw = ImageDraw.Draw(
        image
    )


    font_grid = get_font(20)

    font_det = get_font(17)


    # ========================================================
    # Horizontal
    # ========================================================

    for row in range(4):

        points = [

            grid[
                (row, col)
            ]

            for col in range(4)

        ]


        draw.line(

            [
                (
                    int(x),
                    int(y)
                )

                for x, y
                in points
            ],

            fill="white",

            width=2

        )


    # ========================================================
    # Vertical
    # ========================================================

    for col in range(4):

        points = [

            grid[
                (row, col)
            ]

            for row in range(4)

        ]


        draw.line(

            [
                (
                    int(x),
                    int(y)
                )

                for x, y
                in points
            ],

            fill="white",

            width=2

        )


    # ========================================================
    # Grid Points
    # ========================================================

    for (
        row,
        col
    ), (
        x,
        y
    ) in grid.items():

        main_row, main_col = (

            V2_TO_MAIN_BOARD[
                (row, col)
            ]

        )


        x = int(x)

        y = int(y)


        draw.ellipse(

            (
                x - 6,
                y - 6,
                x + 6,
                y + 6
            ),

            fill="white",

            outline="black"

        )


        draw.text(

            (
                x + 7,
                y + 5
            ),

            (
                f"V2 R{row}C{col}\n"

                f"Main "
                f"R{main_row}C{main_col}"
            ),

            fill="white",

            font=font_grid,

            stroke_width=2,

            stroke_fill="black"

        )


    # ========================================================
    # Detection
    # ========================================================

    for det in detections:

        (
            cell,
            distance
        ) = find_nearest_grid_cell(

            det["cx"],

            det["cy"],

            grid

        )


        local_row, local_col = (
            cell
        )


        gx, gy = (
            grid[cell]
        )


        dx = abs(
            det["cx"] - gx
        )

        dy = abs(
            det["cy"] - gy
        )


        cx = int(
            det["cx"]
        )

        cy = int(
            det["cy"]
        )


        short = (
            det["label"]
            .replace(
                "chess-",
                ""
            )
        )


        if (
            dx <= V2_MAX_DX
            and
            dy <= V2_MAX_DY
        ):

            color = "yellow"

            main_row, main_col = (

                V2_TO_MAIN_BOARD[
                    cell
                ]

            )


            text = (

                f"{short} "

                f"V2[{local_row},{local_col}] "

                f"Main[{main_row},{main_col}] "

                f"dx={dx:.0f} "

                f"dy={dy:.0f}"

            )

        else:

            color = "red"

            text = (

                f"{short} "

                f"IGNORE "

                f"dx={dx:.0f} "

                f"dy={dy:.0f}"

            )


        draw.ellipse(

            (
                cx - 7,
                cy - 7,
                cx + 7,
                cy + 7
            ),

            fill=color,

            outline="black"

        )


        draw.text(

            (
                cx + 8,
                cy - 20
            ),

            text,

            fill=color,

            font=font_det,

            stroke_width=2,

            stroke_fill="black"

        )


    image.save(
        output_path
    )


# ============================================================
# 23. Print Board
# ============================================================

def print_board(
    board,
    title
):

    print()

    print("=" * 155)

    print(title)

    print("=" * 155)


    print(
        "      ",
        end=""
    )


    for col in range(
        BOARD_COLS
    ):

        print(
            f"C{col:^15}",
            end=""
        )


    print()


    for row_index, row in enumerate(
        board
    ):

        print(
            f"R{row_index:<3}",
            end=""
        )


        for piece in row:

            if piece is None:

                text = "."

            else:

                text = (
                    piece
                    .replace(
                        "chess-",
                        ""
                    )
                )


            print(
                f"{text:^16}",
                end=""
            )


        print()


# ============================================================
# 24. Print V2 Patch
# ============================================================

def print_v2_patch(
    patch
):

    print()

    print("=" * 100)

    print(
        "V2 PATCH 4×4"
    )

    print("=" * 100)


    for row_index, row in enumerate(
        patch
    ):

        print(
            f"R{row_index}: ",
            end=""
        )


        for piece in row:

            if piece is None:

                text = "."

            else:

                text = (
                    piece
                    .replace(
                        "chess-",
                        ""
                    )
                )


            print(
                f"{text:^20}",
                end=""
            )


        print()


# ============================================================
# 25. FEN
# ============================================================

def board_to_fen(
    board,
    current_turn
):

    rows = []


    for row in board:

        fen_row = ""

        empty_count = 0


        for piece in row:

            if piece is None:

                empty_count += 1

                continue


            if empty_count > 0:

                fen_row += str(
                    empty_count
                )

                empty_count = 0


            fen_piece = (
                LABEL_TO_FEN.get(
                    piece,
                    "?"
                )
            )


            fen_row += (
                fen_piece
            )


        if empty_count > 0:

            fen_row += str(
                empty_count
            )


        rows.append(
            fen_row
        )


    side = (

        "w"

        if current_turn == "red"

        else "b"

    )


    return (

        "/".join(
            rows
        )

        +

        f" {side} - - 0 1"

    )


# ============================================================
# 26. Main
# ============================================================

def main():

    print()

    print("=" * 120)

    print(
        "Chinese Chess Dual View YOLO"
    )

    print(
        "V1 MANUAL 90-POINT CALIBRATION"
    )

    print(
        "V2 FIXED 2592x1944 CALIBRATION"
    )

    print("=" * 120)


    if not MODEL_PATH.exists():

        raise FileNotFoundError(

            f"找不到模型："
            f"{MODEL_PATH}"

        )


    # ========================================================
    # 找圖
    # ========================================================

    (
        data_folder,
        v1_image,
        v2_image
    ) = find_latest_v1_v2_pair()


    print()

    print(
        f"資料夾：{data_folder}"
    )

    print(
        f"V1：{v1_image.name}"
    )

    print(
        f"V2：{v2_image.name}"
    )


    # ========================================================
    # Image size
    # ========================================================

    with Image.open(
        v1_image
    ) as temp:

        v1_width, v1_height = (
            temp.size
        )


    with Image.open(
        v2_image
    ) as temp:

        v2_width, v2_height = (
            temp.size
        )


    print()

    print(
        f"V1 Size："
        f"{v1_width} × {v1_height}"
    )


    print(
        f"V2 Size："
        f"{v2_width} × {v2_height}"
    )


    # ========================================================
    # Output
    # ========================================================

    output_dir = (

        BASE_DIR

        / "dual_view_results"

        / f"Cycle_{CYCLE_TEXT}"

    )


    output_dir.mkdir(

        parents=True,

        exist_ok=True

    )


    v1_yolo_output = (

        output_dir

        / f"C{CYCLE_TEXT}_V1_YOLO.jpg"

    )


    v2_yolo_output = (

        output_dir

        / f"C{CYCLE_TEXT}_V2_YOLO.jpg"

    )


    v1_grid_output = (

        output_dir

        / f"C{CYCLE_TEXT}_V1_MANUAL_GRID.jpg"

    )


    v2_grid_output = (

        output_dir

        / f"C{CYCLE_TEXT}_V2_GRID_FIXED.jpg"

    )


    # ========================================================
    # Grid
    # ========================================================

    (
        v1_grid,
        _,
        _
    ) = build_v1_manual_grid(

        v1_width,

        v1_height

    )


    (
        v2_grid,
        v2_col_x,
        v2_row_y
    ) = build_v2_grid(

        v2_width,

        v2_height

    )


    print()

    print("=" * 100)

    print(
        "V2 FIXED GRID"
    )

    print("=" * 100)


    print(
        "Columns："
    )


    for col, x in (
        v2_col_x.items()
    ):

        print(

            f"C{col} = "
            f"{x:.1f}"

        )


    print()


    print(
        "Rows："
    )


    for row, y in (
        v2_row_y.items()
    ):

        print(

            f"R{row} = "
            f"{y:.1f}"

        )


    # ========================================================
    # YOLO
    # ========================================================

    model = YOLO(
        str(
            MODEL_PATH
        )
    )


    detections_v1 = run_yolo(

        model,

        v1_image,

        v1_yolo_output,

        "V1"

    )


    detections_v2 = run_yolo(

        model,

        v2_image,

        v2_yolo_output,

        "V2"

    )


    # ========================================================
    # V1
    # ========================================================

    (
        board_v1,
        info_v1,
        skipped_v1,
        ignored_v1
    ) = build_v1_board(

        detections_v1,

        v1_grid

    )


    # ========================================================
    # V2
    # ========================================================

    (
        patch_v2,
        info_v2,
        ignored_v2
    ) = build_v2_patch(

        detections_v2,

        v2_grid

    )


    # ========================================================
    # Final
    # ========================================================

    (
        final_board,
        final_info
    ) = build_final_board(

        board_v1,

        info_v1,

        info_v2

    )


    # ========================================================
    # Draw
    # ========================================================

    draw_v1_grid(

        v1_image,

        detections_v1,

        v1_grid,

        v1_grid_output

    )


    draw_v2_grid(

        v2_image,

        detections_v2,

        v2_grid,

        v2_grid_output

    )


    # ========================================================
    # Matrix
    # ========================================================

    print_board(

        board_v1,

        "V1 MATRIX - V2 AREA EXCLUDED"

    )


    print_v2_patch(
        patch_v2
    )


    print_board(

        final_board,

        "FINAL MATRIX"

    )


    # ========================================================
    # FEN
    # ========================================================

    fen = board_to_fen(

        final_board,

        CURRENT_TURN

    )


    ucci = (
        f"position fen {fen}"
    )


    # ========================================================
    # Summary
    # ========================================================

    print()

    print("=" * 120)

    print(
        "FINAL RESULT"
    )

    print("=" * 120)


    print()

    print(
        f"V1 YOLO Detection："
        f"{len(detections_v1)}"
    )


    print(
        f"V1 正式使用："
        f"{len(info_v1)}"
    )


    print(
        f"V1 → V2："
        f"{len(skipped_v1)}"
    )


    print(
        f"V1 Ignore："
        f"{len(ignored_v1)}"
    )


    print()

    print(
        f"V2 YOLO Detection："
        f"{len(detections_v2)}"
    )


    print(
        f"V2 有效 Detection："
        f"{len(info_v2)}"
    )


    print(
        f"V2 Ignore："
        f"{len(ignored_v2)}"
    )


    print()

    print(
        f"Final 有棋子格數："
        f"{len(final_info)}"
    )


    print()

    print(
        "FEN："
    )

    print(
        fen
    )


    print()

    print(
        "UCCI："
    )

    print(
        ucci
    )


    print()

    print(
        "輸出圖片："
    )


    print(
        f"V1 Grid："
        f"{v1_grid_output}"
    )


    print(
        f"V2 Grid："
        f"{v2_grid_output}"
    )


    print()

    print("=" * 120)


# ============================================================
# 27. Start
# ============================================================

if __name__ == "__main__":

    main()