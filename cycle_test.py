from ultralytics import YOLO

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

import csv
import math
from datetime import datetime


# ============================================================
# 1. 基本設定
# ============================================================

BASE_DIR = Path(
    r"C:\Users\user\Desktop\專題\專題程式\test"
)

MODEL_PATH = BASE_DIR / "best.pt"


# ============================================================
# 2. 要分析的 Cycle
#
# 例如：
# Cycle 1 → 1
# Cycle 25 → 25
# ============================================================

CYCLE = 1

CYCLE_TEXT = f"{CYCLE:03d}"


# ============================================================
# 3. YOLO
# ============================================================

CONF_THRESHOLD = 0.25


# ============================================================
# 4. 棋盤大小
#
# 中國象棋：
#
# 10 Row
# 9 Column
# ============================================================

BOARD_ROWS = 10
BOARD_COLS = 9


# ============================================================
# 5. 玩家回合
#
# red   → FEN w
# black → FEN b
# ============================================================

CURRENT_TURN = "red"


# ============================================================
# 6. V1 人工校正
#
# 這組座標來自你人工畫出的90個交叉點。
#
# 原人工校正圖片：
# 2048 × 1536
#
# 正式 V1 如果是2592×1944，
# 程式會自動 Scale。
# ============================================================

V1_CALIBRATION_WIDTH = 2048
V1_CALIBRATION_HEIGHT = 1536


# ============================================================
# V1 Columns
# ============================================================

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


# ============================================================
# V1 Rows
#
# R4 → 上半最後一列
# R5 → 下半第一列
#
# 楚河漢界直接保留，
# 不做等距推算。
# ============================================================

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


# ============================================================
# V1 最大 Grid 距離
#
# 棋子中心距離最近交叉點超過此值：
# → IGNORE
#
# 因為你的部分棋子沒有完全放在交叉線中央，
# 目前先保留105。
# ============================================================

V1_MAX_GRID_DISTANCE = 105.0


# ============================================================
# 7. V2 正式校正
#
# ★ 使用你目前最新確認的數值
#
# 這組座標本身就是建立在：
#
# 2592 × 1944
#
# 的 V2 影像上。
# ============================================================

V2_CALIBRATION_WIDTH = 2592
V2_CALIBRATION_HEIGHT = 1944


# ============================================================
# V2 Columns
# ============================================================

V2_CALIBRATION_COL_X = {

    0: 1252.0,

    1: 1506.0,

    2: 1765.0,

    3: 2025.0,
}


# ============================================================
# V2 Rows
# ============================================================

V2_CALIBRATION_ROW_Y = {

    0: 929.0,

    1: 1189.0,

    2: 1430.0,

    3: 1690.0,
}


# ============================================================
# V2允許距離
#
# 分開判斷 X / Y。
# ============================================================

V2_MAX_DX = 125.0
V2_MAX_DY = 125.0


# ============================================================
# 8. V2 → Main Board
#
# V2 完整接管：
#
# Main R1~R4
# Main C0~C3
#
# 共 4 × 4 = 16 格
# ============================================================

V2_TO_MAIN_BOARD = {

    # --------------------------------------------------------
    # V2 R0 → Main R1
    # --------------------------------------------------------

    (0, 0): (1, 0),

    (0, 1): (1, 1),

    (0, 2): (1, 2),

    (0, 3): (1, 3),


    # --------------------------------------------------------
    # V2 R1 → Main R2
    # --------------------------------------------------------

    (1, 0): (2, 0),

    (1, 1): (2, 1),

    (1, 2): (2, 2),

    (1, 3): (2, 3),


    # --------------------------------------------------------
    # V2 R2 → Main R3
    # --------------------------------------------------------

    (2, 0): (3, 0),

    (2, 1): (3, 1),

    (2, 2): (3, 2),

    (2, 3): (3, 3),


    # --------------------------------------------------------
    # V2 R3 → Main R4
    # --------------------------------------------------------

    (3, 0): (4, 0),

    (3, 1): (4, 1),

    (3, 2): (4, 2),

    (3, 3): (4, 3),
}


V2_MAIN_CELLS = set(
    V2_TO_MAIN_BOARD.values()
)


# ============================================================
# 9. FEN Mapping
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
# 10. Ground Truth 評估
#
# False：
# 不做真實正確率評估
#
# True：
# 使用下面 EXPECTED_BOARD 跟 Final Matrix 比較
#
# 等你正式固定棋盤初始位置後，
# 可以改 True。
# ============================================================

ENABLE_GROUND_TRUTH_EVALUATION = False


# ============================================================
# Ground Truth
#
# None = 空格
#
# 如果要正式算 Accuracy，
# 把每個實際棋子 Label 填進去。
#
# 注意：
# 這裡只是模板，目前不啟用。
# ============================================================

EXPECTED_BOARD = [

    # R0
    [
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None
    ],

    # R1
    [
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None
    ],

    # R2
    [
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None
    ],

    # R3
    [
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None
    ],

    # R4
    [
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None
    ],

    # R5
    [
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None
    ],

    # R6
    [
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None
    ],

    # R7
    [
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None
    ],

    # R8
    [
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None
    ],

    # R9
    [
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None
    ],
]


# ============================================================
# 11. 找同一組 V1 / V2
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

            f"同一資料夾中的 V1 / V2 圖片"

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

        key=lambda p:
            p.stat().st_mtime

    )


    v2_image = max(

        v2_files,

        key=lambda p:
            p.stat().st_mtime

    )


    return (

        latest_folder,

        v1_image,

        v2_image

    )


# ============================================================
# 12. 建立空棋盤
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
# 13. Scale X
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
# 14. Scale Y
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
# 15. V1 Grid
# ============================================================

def build_v1_grid(
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
# 16. V2 Grid
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
# 17. YOLO
# ============================================================

def run_yolo(
    model,
    image_path,
    output_path,
    view_name
):

    print()

    print("=" * 120)

    print(
        f"{view_name} YOLO"
    )

    print(
        image_path
    )

    print("=" * 120)


    results = model.predict(

        source=str(
            image_path
        ),

        conf=CONF_THRESHOLD,

        verbose=False

    )


    result = results[0]


    # ========================================================
    # 儲存YOLO原生結果圖
    # ========================================================

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

        print(
            "沒有 Detection"
        )

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

            "index":
                i,

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
# 18. 找最近 Grid
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
# 19. V1 Mapping
# ============================================================

def build_v1_board(
    detections,
    grid
):

    board = (
        create_empty_board()
    )


    info = {}


    mapping_records = []


    stats = {

        "detections":
            len(detections),

        "mapped":
            0,

        "v2_reserved":
            0,

        "outside":
            0,

        "collision":
            0,

        "replaced":
            0,
    }


    print()

    print("=" * 120)

    print(
        "V1 → 90 GRID MAPPING"
    )

    print("=" * 120)


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


        gx, gy = (
            grid[
                cell
            ]
        )


        dx = abs(
            det["cx"] - gx
        )


        dy = abs(
            det["cy"] - gy
        )


        # ====================================================
        # 太遠
        # ====================================================

        if (
            distance
            >
            V1_MAX_GRID_DISTANCE
        ):

            status = (
                "IGNORE_OUTSIDE_GRID"
            )


            stats[
                "outside"
            ] += 1


            mapping_records.append({

                **det,

                "view":
                    "V1",

                "local_row":
                    "",

                "local_col":
                    "",

                "main_row":
                    row,

                "main_col":
                    col,

                "grid_x":
                    gx,

                "grid_y":
                    gy,

                "dx":
                    dx,

                "dy":
                    dy,

                "distance":
                    distance,

                "status":
                    status,

            })


            print(

                f"[IGNORE] "

                f"{det['label']:<25} "

                f"Nearest="
                f"R{row}C{col} "

                f"d="
                f"{distance:.1f}"

            )


            continue


        # ====================================================
        # V2 16格
        #
        # V1不使用，
        # 後面交給V2完整接管。
        # ====================================================

        if (
            cell
            in
            V2_MAIN_CELLS
        ):

            status = (
                "RESERVED_FOR_V2"
            )


            stats[
                "v2_reserved"
            ] += 1


            mapping_records.append({

                **det,

                "view":
                    "V1",

                "local_row":
                    "",

                "local_col":
                    "",

                "main_row":
                    row,

                "main_col":
                    col,

                "grid_x":
                    gx,

                "grid_y":
                    gy,

                "dx":
                    dx,

                "dy":
                    dy,

                "distance":
                    distance,

                "status":
                    status,

            })


            print(

                f"[V2 RESERVED] "

                f"{det['label']:<25} "

                f"→ R{row}C{col}"

            )


            continue


        # ====================================================
        # Collision
        # ====================================================

        old = (
            info.get(
                cell
            )
        )


        if old is not None:

            stats[
                "collision"
            ] += 1


            # =================================================
            # 新 Detection confidence 更高
            # =================================================

            if (
                det["confidence"]
                >
                old["confidence"]
            ):

                status = (
                    "MAPPED_REPLACE_OLD"
                )


                stats[
                    "replaced"
                ] += 1


                board[
                    row
                ][
                    col
                ] = (
                    det["label"]
                )


                info[
                    cell
                ] = {

                    **det,

                    "row":
                        row,

                    "col":
                        col,

                    "grid_x":
                        gx,

                    "grid_y":
                        gy,

                    "dx":
                        dx,

                    "dy":
                        dy,

                    "distance":
                        distance,

                    "source":
                        "V1"

                }


            else:

                status = (
                    "IGNORE_COLLISION_LOWER_CONF"
                )


                mapping_records.append({

                    **det,

                    "view":
                        "V1",

                    "local_row":
                        "",

                    "local_col":
                        "",

                    "main_row":
                        row,

                    "main_col":
                        col,

                    "grid_x":
                        gx,

                    "grid_y":
                        gy,

                    "dx":
                        dx,

                    "dy":
                        dy,

                    "distance":
                        distance,

                    "status":
                        status,

                })


                continue


        else:

            status = (
                "MAPPED"
            )


            board[
                row
            ][
                col
            ] = (
                det["label"]
            )


            info[
                cell
            ] = {

                **det,

                "row":
                    row,

                "col":
                    col,

                "grid_x":
                    gx,

                "grid_y":
                    gy,

                "dx":
                    dx,

                "dy":
                    dy,

                "distance":
                    distance,

                "source":
                    "V1"

            }


        stats[
            "mapped"
        ] += 1


        mapping_records.append({

            **det,

            "view":
                "V1",

            "local_row":
                "",

            "local_col":
                "",

            "main_row":
                row,

            "main_col":
                col,

            "grid_x":
                gx,

            "grid_y":
                gy,

            "dx":
                dx,

            "dy":
                dy,

            "distance":
                distance,

            "status":
                status,

        })


        print(

            f"{det['label']:<25} "

            f"→ R{row}C{col} "

            f"d="
            f"{distance:.1f}"

        )


    return (
        board,
        info,
        mapping_records,
        stats
    )


# ============================================================
# 20. V2 Mapping
# ============================================================

def build_v2_board(
    detections,
    grid
):

    patch = [

        [
            None
            for _ in range(
                4
            )
        ]

        for _ in range(
            4
        )

    ]


    info = {}


    mapping_records = []


    stats = {

        "detections":
            len(detections),

        "mapped":
            0,

        "outside":
            0,

        "collision":
            0,

        "replaced":
            0,
    }


    print()

    print("=" * 120)

    print(
        "V2 → 4×4 GRID MAPPING"
    )

    print("=" * 120)


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


        main_row, main_col = (

            V2_TO_MAIN_BOARD[
                local_cell
            ]

        )


        # ====================================================
        # 區域外
        # ====================================================

        if (
            dx > V2_MAX_DX
            or
            dy > V2_MAX_DY
        ):

            stats[
                "outside"
            ] += 1


            status = (
                "IGNORE_OUTSIDE_V2"
            )


            mapping_records.append({

                **det,

                "view":
                    "V2",

                "local_row":
                    local_row,

                "local_col":
                    local_col,

                "main_row":
                    main_row,

                "main_col":
                    main_col,

                "grid_x":
                    gx,

                "grid_y":
                    gy,

                "dx":
                    dx,

                "dy":
                    dy,

                "distance":
                    distance,

                "status":
                    status,

            })


            print(

                f"[IGNORE] "

                f"{det['label']:<25} "

                f"Nearest="
                f"V2 R{local_row}C{local_col} "

                f"dx="
                f"{dx:.1f} "

                f"dy="
                f"{dy:.1f}"

            )


            continue


        # ====================================================
        # Collision
        # ====================================================

        old = (
            info.get(
                local_cell
            )
        )


        if old is not None:

            stats[
                "collision"
            ] += 1


            if (
                det["confidence"]
                >
                old["confidence"]
            ):

                stats[
                    "replaced"
                ] += 1


                status = (
                    "MAPPED_REPLACE_OLD"
                )


                patch[
                    local_row
                ][
                    local_col
                ] = (
                    det["label"]
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

                    "grid_x":
                        gx,

                    "grid_y":
                        gy,

                    "dx":
                        dx,

                    "dy":
                        dy,

                    "distance":
                        distance,

                    "source":
                        "V2"

                }


            else:

                status = (
                    "IGNORE_COLLISION_LOWER_CONF"
                )


                mapping_records.append({

                    **det,

                    "view":
                        "V2",

                    "local_row":
                        local_row,

                    "local_col":
                        local_col,

                    "main_row":
                        main_row,

                    "main_col":
                        main_col,

                    "grid_x":
                        gx,

                    "grid_y":
                        gy,

                    "dx":
                        dx,

                    "dy":
                        dy,

                    "distance":
                        distance,

                    "status":
                        status,

                })


                continue


        else:

            status = (
                "MAPPED"
            )


            patch[
                local_row
            ][
                local_col
            ] = (
                det["label"]
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

                "grid_x":
                    gx,

                "grid_y":
                    gy,

                "dx":
                    dx,

                "dy":
                    dy,

                "distance":
                    distance,

                "source":
                    "V2"

            }


        stats[
            "mapped"
        ] += 1


        mapping_records.append({

            **det,

            "view":
                "V2",

            "local_row":
                local_row,

            "local_col":
                local_col,

            "main_row":
                main_row,

            "main_col":
                main_col,

            "grid_x":
                gx,

            "grid_y":
                gy,

            "dx":
                dx,

            "dy":
                dy,

            "distance":
                distance,

            "status":
                status,

        })


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
        mapping_records,
        stats
    )


# ============================================================
# 21. V1 + V2 Fusion
#
# V2 完整接管16格。
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
    # 先把16格全部清空
    #
    # 代表：
    #
    # V2沒有看到棋子
    # → 那格就視為空格
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
    # V2填入
    # ========================================================

    for local_cell, det in (
        info_v2.items()
    ):

        row = (
            det[
                "main_row"
            ]
        )


        col = (
            det[
                "main_col"
            ]
        )


        final_board[
            row
        ][
            col
        ] = (
            det[
                "label"
            ]
        )


        final_info[
            (row, col)
        ] = {

            **det,

            "source":
                "V2_AUTHORITATIVE"

        }


    return (
        final_board,
        final_info
    )


# ============================================================
# 22. FEN
# ============================================================

def board_to_fen(
    board,
    current_turn
):

    fen_rows = []


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
                    piece
                )
            )


            if fen_piece is None:

                fen_piece = "?"


                print(

                    f"[WARNING] "

                    f"Unknown Label："

                    f"{piece}"

                )


            fen_row += (
                fen_piece
            )


        if empty_count > 0:

            fen_row += str(
                empty_count
            )


        fen_rows.append(
            fen_row
        )


    side = (

        "w"

        if current_turn == "red"

        else "b"

    )


    return (

        "/".join(
            fen_rows
        )

        +

        f" {side} - - 0 1"

    )


# ============================================================
# 23. Print Matrix
# ============================================================

def print_board(
    board,
    title
):

    print()

    print("=" * 160)

    print(
        title
    )

    print("=" * 160)


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

    print("=" * 110)

    print(
        "V2 PATCH 4 × 4"
    )

    print("=" * 110)


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
                f"{text:^22}",
                end=""
            )


        print()


# ============================================================
# 25. Font
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
# 26. Draw V1 Mapping
# ============================================================

def draw_v1_mapping(
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


    font_grid = get_font(
        15
    )


    font_det = get_font(
        17
    )


    # ========================================================
    # Horizontal Lines
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
    #
    # 楚河漢界分開畫
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
    # 90 Grid Points
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


        if (
            distance
            >
            V1_MAX_GRID_DISTANCE
        ):

            color = "red"

            target = "IGNORE"

        elif (
            cell
            in
            V2_MAIN_CELLS
        ):

            color = "cyan"

            target = "V2"

        else:

            color = "yellow"

            target = (
                f"R{row}C{col}"
            )


        short_label = (

            det[
                "label"
            ]

            .replace(
                "chess-",
                ""
            )

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

                f"{short_label} "

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
# 27. Draw V2 Mapping
# ============================================================

def draw_v2_mapping(
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


    font_grid = get_font(
        19
    )


    font_det = get_font(
        16
    )


    # ========================================================
    # Horizontal
    # ========================================================

    for row in range(
        4
    ):

        points = [

            grid[
                (row, col)
            ]

            for col in range(
                4
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
        4
    ):

        points = [

            grid[
                (row, col)
            ]

            for row in range(
                4
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
                f"Main R{main_row}C{main_col}"
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
            grid[
                cell
            ]
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


        short_label = (

            det[
                "label"
            ]

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

                f"{short_label} "

                f"V2[{local_row},{local_col}] "

                f"Main[{main_row},{main_col}] "

                f"dx={dx:.0f} "

                f"dy={dy:.0f}"

            )

        else:

            color = "red"


            text = (

                f"{short_label} "

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
# 28. Detection Mapping CSV
# ============================================================

def save_mapping_csv(
    records,
    output_path
):

    fieldnames = [

        "view",

        "index",

        "label",

        "confidence",

        "x1",

        "y1",

        "x2",

        "y2",

        "cx",

        "cy",

        "local_row",

        "local_col",

        "main_row",

        "main_col",

        "grid_x",

        "grid_y",

        "dx",

        "dy",

        "distance",

        "status",
    ]


    with open(

        output_path,

        "w",

        newline="",

        encoding="utf-8-sig"

    ) as f:

        writer = csv.DictWriter(

            f,

            fieldnames=fieldnames

        )


        writer.writeheader()


        for record in records:

            row = {}


            for field in fieldnames:

                value = (
                    record.get(
                        field,
                        ""
                    )
                )


                if isinstance(
                    value,
                    float
                ):

                    value = round(
                        value,
                        4
                    )


                row[
                    field
                ] = value


            writer.writerow(
                row
            )


# ============================================================
# 29. Ground Truth Evaluation
# ============================================================

def evaluate_board(
    predicted_board,
    expected_board
):

    total_cells = (
        BOARD_ROWS
        *
        BOARD_COLS
    )


    correct_cells = 0

    incorrect_cells = 0


    expected_piece_count = 0

    correctly_recognized_pieces = 0


    errors = []


    for row in range(
        BOARD_ROWS
    ):

        for col in range(
            BOARD_COLS
        ):

            predicted = (
                predicted_board[
                    row
                ][
                    col
                ]
            )


            expected = (
                expected_board[
                    row
                ][
                    col
                ]
            )


            if expected is not None:

                expected_piece_count += 1


            if (
                predicted
                ==
                expected
            ):

                correct_cells += 1


                if expected is not None:

                    correctly_recognized_pieces += 1


            else:

                incorrect_cells += 1


                errors.append({

                    "row":
                        row,

                    "col":
                        col,

                    "expected":
                        expected,

                    "predicted":
                        predicted,

                })


    cell_accuracy = (

        correct_cells
        /
        total_cells
        *
        100

    )


    if (
        expected_piece_count
        >
        0
    ):

        piece_accuracy = (

            correctly_recognized_pieces
            /
            expected_piece_count
            *
            100

        )

    else:

        piece_accuracy = 0.0


    complete_board_success = (

        incorrect_cells
        ==
        0

    )


    return {

        "total_cells":
            total_cells,

        "correct_cells":
            correct_cells,

        "incorrect_cells":
            incorrect_cells,

        "expected_piece_count":
            expected_piece_count,

        "correctly_recognized_pieces":
            correctly_recognized_pieces,

        "cell_accuracy":
            cell_accuracy,

        "piece_accuracy":
            piece_accuracy,

        "complete_board_success":
            complete_board_success,

        "errors":
            errors,

    }


# ============================================================
# 30. Summary CSV
# ============================================================

def save_summary_csv(
    output_path,
    v1_stats,
    v2_stats,
    final_piece_count,
    fen,
    ground_truth_result=None
):

    file_exists = (
        output_path.exists()
    )


    fieldnames = [

        "timestamp",

        "cycle",

        "v1_detection",

        "v1_mapped",

        "v1_reserved_v2",

        "v1_outside",

        "v1_collision",

        "v2_detection",

        "v2_mapped",

        "v2_outside",

        "v2_collision",

        "final_piece_count",

        "cell_accuracy",

        "piece_accuracy",

        "complete_board_success",

        "fen",
    ]


    row = {

        "timestamp":
            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

        "cycle":
            CYCLE,

        "v1_detection":
            v1_stats[
                "detections"
            ],

        "v1_mapped":
            v1_stats[
                "mapped"
            ],

        "v1_reserved_v2":
            v1_stats[
                "v2_reserved"
            ],

        "v1_outside":
            v1_stats[
                "outside"
            ],

        "v1_collision":
            v1_stats[
                "collision"
            ],

        "v2_detection":
            v2_stats[
                "detections"
            ],

        "v2_mapped":
            v2_stats[
                "mapped"
            ],

        "v2_outside":
            v2_stats[
                "outside"
            ],

        "v2_collision":
            v2_stats[
                "collision"
            ],

        "final_piece_count":
            final_piece_count,

        "cell_accuracy":
            "",

        "piece_accuracy":
            "",

        "complete_board_success":
            "",

        "fen":
            fen,
    }


    if (
        ground_truth_result
        is not None
    ):

        row[
            "cell_accuracy"
        ] = round(

            ground_truth_result[
                "cell_accuracy"
            ],

            4

        )


        row[
            "piece_accuracy"
        ] = round(

            ground_truth_result[
                "piece_accuracy"
            ],

            4

        )


        row[
            "complete_board_success"
        ] = (

            1

            if ground_truth_result[
                "complete_board_success"
            ]

            else 0

        )


    with open(

        output_path,

        "a",

        newline="",

        encoding="utf-8-sig"

    ) as f:

        writer = csv.DictWriter(

            f,

            fieldnames=fieldnames

        )


        if not file_exists:

            writer.writeheader()


        writer.writerow(
            row
        )


# ============================================================
# 31. Main
# ============================================================

def main():

    print()

    print("=" * 130)

    print(
        "Chinese Chess Dual View Recognition System"
    )

    print()

    print(
        "V1 = Manual 90 Point Calibration"
    )

    print(
        "V2 = Authoritative 4x4 / 16 Cells"
    )

    print(
        "Final = V1 + V2 Fusion + FEN"
    )

    print("=" * 130)


    # ========================================================
    # Model
    # ========================================================

    if not MODEL_PATH.exists():

        raise FileNotFoundError(

            f"找不到模型：\n"

            f"{MODEL_PATH}"

        )


    # ========================================================
    # 找同一組圖片
    # ========================================================

    (
        source_folder,

        v1_image,

        v2_image

    ) = find_latest_v1_v2_pair()


    print()

    print(
        f"Cycle：{CYCLE}"
    )


    print(
        f"來源資料夾："
        f"{source_folder}"
    )


    print(
        f"V1："
        f"{v1_image.name}"
    )


    print(
        f"V2："
        f"{v2_image.name}"
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


    v1_mapping_output = (

        output_dir

        / f"C{CYCLE_TEXT}_V1_MAPPING.jpg"

    )


    v2_mapping_output = (

        output_dir

        / f"C{CYCLE_TEXT}_V2_MAPPING.jpg"

    )


    mapping_csv = (

        output_dir

        / f"C{CYCLE_TEXT}_Mapping.csv"

    )


    summary_csv = (

        BASE_DIR

        / "dual_view_results"

        / "Recognition_Summary.csv"

    )


    # ========================================================
    # Image Size
    # ========================================================

    with Image.open(
        v1_image
    ) as img:

        v1_width, v1_height = (
            img.size
        )


    with Image.open(
        v2_image
    ) as img:

        v2_width, v2_height = (
            img.size
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
    # Grid
    # ========================================================

    (
        v1_grid,
        v1_col_x,
        v1_row_y

    ) = build_v1_grid(

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


    # ========================================================
    # 印出V2正式Grid
    # ========================================================

    print()

    print("=" * 80)

    print(
        "V2 CALIBRATION"
    )

    print("=" * 80)


    print(
        "Column X："
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
        "Row Y："
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
    # V1 Mapping
    # ========================================================

    (
        board_v1,

        info_v1,

        records_v1,

        stats_v1

    ) = build_v1_board(

        detections_v1,

        v1_grid

    )


    # ========================================================
    # V2 Mapping
    # ========================================================

    (
        patch_v2,

        info_v2,

        records_v2,

        stats_v2

    ) = build_v2_board(

        detections_v2,

        v2_grid

    )


    # ========================================================
    # Final Fusion
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
    # Matrix
    # ========================================================

    print_board(

        board_v1,

        "V1 MATRIX"
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

        f"position fen "

        f"{fen}"

    )


    # ========================================================
    # Debug Image
    # ========================================================

    draw_v1_mapping(

        v1_image,

        detections_v1,

        v1_grid,

        v1_mapping_output

    )


    draw_v2_mapping(

        v2_image,

        detections_v2,

        v2_grid,

        v2_mapping_output

    )


    # ========================================================
    # Mapping CSV
    # ========================================================

    all_records = (

        records_v1

        +

        records_v2

    )


    save_mapping_csv(

        all_records,

        mapping_csv

    )


    # ========================================================
    # Ground Truth
    # ========================================================

    ground_truth_result = None


    if (
        ENABLE_GROUND_TRUTH_EVALUATION
    ):

        ground_truth_result = (
            evaluate_board(

                final_board,

                EXPECTED_BOARD

            )
        )


    # ========================================================
    # Summary CSV
    # ========================================================

    save_summary_csv(

        summary_csv,

        stats_v1,

        stats_v2,

        len(
            final_info
        ),

        fen,

        ground_truth_result

    )


    # ========================================================
    # Summary
    # ========================================================

    print()

    print("=" * 130)

    print(
        "FINAL RESULT"
    )

    print("=" * 130)


    print()

    print(
        "【V1】"
    )


    print(

        f"YOLO Detection："

        f"{stats_v1['detections']}"

    )


    print(

        f"正式 Mapping："

        f"{stats_v1['mapped']}"

    )


    print(

        f"交給 V2 接管："

        f"{stats_v1['v2_reserved']}"

    )


    print(

        f"Grid 外 Ignore："

        f"{stats_v1['outside']}"

    )


    print(

        f"Collision："

        f"{stats_v1['collision']}"

    )


    print()

    print(
        "【V2】"
    )


    print(

        f"YOLO Detection："

        f"{stats_v2['detections']}"

    )


    print(

        f"成功 Mapping："

        f"{stats_v2['mapped']}"

    )


    print(

        f"區域外 Ignore："

        f"{stats_v2['outside']}"

    )


    print(

        f"Collision："

        f"{stats_v2['collision']}"

    )


    # ========================================================
    # Mapping Completion
    #
    # 注意：
    # 這不是 Ground Truth Accuracy。
    # ========================================================

    valid_v2_attempts = (

        stats_v2[
            "mapped"
        ]

        +

        stats_v2[
            "collision"
        ]

    )


    if (
        valid_v2_attempts
        >
        0
    ):

        v2_mapping_completion = (

            stats_v2[
                "mapped"
            ]

            /
            valid_v2_attempts

            *
            100

        )

    else:

        v2_mapping_completion = 0


    print()

    print(

        f"V2 Mapping Completion："

        f"{v2_mapping_completion:.2f}%"

    )


    print()

    print(
        "【Final】"
    )


    print(

        f"Final 棋子格數："

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


    # ========================================================
    # Ground Truth Result
    # ========================================================

    if (
        ground_truth_result
        is not None
    ):

        print()

        print(
            "【Ground Truth Evaluation】"
        )


        print(

            f"Cell Accuracy："

            f"{ground_truth_result['cell_accuracy']:.2f}%"

        )


        print(

            f"Piece Accuracy："

            f"{ground_truth_result['piece_accuracy']:.2f}%"

        )


        print(

            f"Complete Board Success："

            f"{ground_truth_result['complete_board_success']}"

        )


        if (
            ground_truth_result[
                "errors"
            ]
        ):

            print()

            print(
                "錯誤格位："
            )


            for error in (
                ground_truth_result[
                    "errors"
                ]
            ):

                print(

                    f"R{error['row']}"

                    f"C{error['col']} "

                    f"Expected="

                    f"{error['expected']} "

                    f"Predicted="

                    f"{error['predicted']}"

                )


    # ========================================================
    # Output
    # ========================================================

    print()

    print(
        "【輸出】"
    )


    print(

        f"V1 YOLO：\n"

        f"{v1_yolo_output}"

    )


    print(

        f"V1 Mapping：\n"

        f"{v1_mapping_output}"

    )


    print(

        f"V2 YOLO：\n"

        f"{v2_yolo_output}"

    )


    print(

        f"V2 Mapping：\n"

        f"{v2_mapping_output}"

    )


    print(

        f"Mapping CSV：\n"

        f"{mapping_csv}"

    )


    print(

        f"Summary CSV：\n"

        f"{summary_csv}"

    )


    print()

    print("=" * 130)

    print(
        "完成"
    )

    print("=" * 130)


# ============================================================
# 32. Start
# ============================================================

if __name__ == "__main__":

    main()  