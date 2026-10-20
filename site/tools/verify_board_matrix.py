"""Dry-run or execute the operator-supervised TM5-700 90-point inspection."""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

SITE_ROOT = Path(__file__).resolve().parents[1]
if str(SITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SITE_ROOT))

from tmchess.calibration import (  # noqa: E402
    BoardMatrixInspector,
    InspectionProfile,
    InspectionRecord,
    build_inspection_points,
)
from tmchess.config import CONFIG  # noqa: E402
from tmchess.robot import TMRobotAdapter  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TM5-700 棋盤 9×10 點位巡檢（預設只顯示路徑）")
    parser.add_argument("--execute", action="store_true", help="實際送出真機移動指令")
    parser.add_argument(
        "--allow-below-configured-safe-z",
        action="store_true",
        help="允許以 Z=380 跨點；此高度低於正式 Z_SAFE=525",
    )
    parser.add_argument("--start-point", type=int, default=1, help="起始序號，1–90")
    parser.add_argument("--end-point", type=int, default=90, help="結束序號，1–90")
    parser.add_argument("--step-confirm", action="store_true", help="每一點移動前都等待操作員按 Enter")
    parser.add_argument("--travel-speed", type=int, default=20)
    parser.add_argument("--approach-speed", type=int, default=10)
    parser.add_argument("--inspection-speed", type=int, default=5)
    parser.add_argument("--acceleration", type=int, default=100)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    points = build_inspection_points(CONFIG.motion)
    if not (1 <= args.start_point <= args.end_point <= len(points)):
        raise SystemExit("點位範圍必須符合 1 <= start-point <= end-point <= 90。")
    selected = points[args.start_point - 1:args.end_point]
    profile = InspectionProfile(
        travel_speed=args.travel_speed,
        approach_speed=args.approach_speed,
        inspection_speed=args.inspection_speed,
        acceleration=args.acceleration,
    )

    print("巡檢路徑：Z_SAFE 進場 → Z380 跨點 → Z340 → 低速 Z300 → 停留 3 秒 → Z380")
    print(f"速度：跨點 {profile.travel_speed}／接近 {profile.approach_speed}／下降 {profile.inspection_speed}；加速度 {profile.acceleration}")
    for point in selected:
        print(f"{point.label}: X={point.x:.3f}, Y={point.y:.3f}")
    if not args.execute:
        print("\n乾跑完成：未連接機械手臂，也未送出移動命令。")
        print("建議先執行單點：--execute --start-point 1 --end-point 1 --allow-below-configured-safe-z")
        return 0

    if not CONFIG.safety.hardware_enabled:
        raise SystemExit("HARDWARE_ENABLED 尚未啟用。")
    profile.validate(CONFIG.motion, allow_below_safe_z=args.allow_below_configured_safe_z)
    print("\n警告：這會實際移動 TM5-700。棋盤可保留正常平放的單層棋子，但不得堆疊、翹起或放置其他異物。")
    print("機械手臂運動範圍內不得有人；棋子頂面至 Z=300 的名義間隙約 9.7 mm。")
    print("網站停止不能取代實體 Emergency Stop；操作員必須全程握有急停控制權。")
    print("Ctrl+C 只能阻止後續命令，不能保證立即停止已送出的當前運動。")
    confirmation = input('確認 Base/TCP 正確、棋子皆正常平放且路徑無異物，輸入 "MOVE 90 POINTS"：').strip()
    if confirmation != "MOVE 90 POINTS":
        print("確認文字不符，未執行。")
        return 2

    output_dir = SITE_ROOT / "runtime" / "calibration"
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / f"board-matrix-{datetime.now():%Y%m%d-%H%M%S}.csv"
    robot = TMRobotAdapter(CONFIG.network, CONFIG.motion)
    inspector = BoardMatrixInspector(
        robot,
        CONFIG.motion,
        profile,
        allow_below_safe_z=args.allow_below_configured_safe_z,
    )

    with log_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow([
            "point", "player_row", "player_col", "target_x", "target_y", "target_z",
            "actual_x", "actual_y", "actual_z", "actual_rx", "actual_ry", "actual_rz", "xyz_error_mm",
        ])

        def before_point(point):
            if args.step_confirm:
                input(f"{point.label}：確認路徑安全後按 Enter，Ctrl+C 停止。")
            else:
                print(f"移動至 {point.label}…")

        def record(result: InspectionRecord):
            point, target, actual = result.point, result.target, result.actual
            writer.writerow([
                point.number, point.player_row + 1, point.player_col + 1,
                *[f"{value:.3f}" for value in target[:3]],
                *[f"{value:.3f}" for value in actual], f"{result.error_mm:.3f}",
            ])
            stream.flush()
            print(f"  實際 XYZ=({actual[0]:.3f}, {actual[1]:.3f}, {actual[2]:.3f})，誤差={result.error_mm:.3f} mm")

        try:
            inspector.run(selected, record, before_point=before_point)
        except KeyboardInterrupt:
            print("\n操作員停止：不再送出後續命令；當前已送出的運動仍可能繼續。必要時請使用實體 Emergency Stop。")
            return 130
        except Exception as exc:
            print(f"\n巡檢失敗：{exc}")
            print("已停止後續命令；請先確認機械手臂狀態，不要直接重跑。")
            return 1

    print(f"\n巡檢完成，結果：{log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
