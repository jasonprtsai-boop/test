"""TMflow 手臂相機畫面截取並透過 FTPS 上傳測試腳本。

說明：
1. 向 TMflow 手臂視覺伺服器 (預設 Port 8080) 請求最新即時影像。
2. 將獲取的影像資料透過顯式 FTPS (TLS Session Resumption) 上傳至本機 FileZilla Server。
"""

import io
import os
import ssl
from datetime import datetime
from ftplib import FTP, FTP_TLS
import requests

# 🎯 保持與新版 FileZilla 的安全通訊協定 (TLS Session Reuse)
# FileZilla Server 要求資料傳輸通道必須重用控制通道的 TLS Session
class ReusedTLS_FTP(FTP_TLS):
    def ntransfercmd(self, cmd, rest=None):
        conn, size = FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            session = self.sock.session if isinstance(self.sock, ssl.SSLSocket) else None
            conn = self.context.wrap_socket(
                conn,
                server_hostname=self.host,
                session=session,
            )
        return conn, size

# 👁️ TMflow 內建的視覺網頁即時畫面網址 (Port 8080)
# 請依實際手臂網路設定確認 IP (例如 192.168.10.50 或 192.168.1.2)
ROBOT_CAMERA_URL = os.getenv("ROBOT_CAMERA_URL", "http://192.168.10.50:8080")

# FTPS 伺服器設定
FTPS_HOST = os.getenv("FTPS_HOST", "127.0.0.1")
FTPS_PORT = int(os.getenv("FTPS_PORT", "21"))
FTPS_USER = os.getenv("FTPS_USER", "tm_user")
FTPS_PASSWORD = os.getenv("FTPS_PASSWORD", "您的密碼")


def fetch_and_upload_image(
    camera_url: str = ROBOT_CAMERA_URL,
    ftp_host: str = FTPS_HOST,
    ftp_port: int = FTPS_PORT,
    ftp_user: str = FTPS_USER,
    ftp_password: str = FTPS_PASSWORD,
) -> bool:
    ftps = None
    try:
        # 1. 向手臂通訊，抓取當下相機的最新一張原始相片
        print(f"正在向手臂相機請求即時影像 ({camera_url})...")
        response = requests.get(camera_url, timeout=3)

        if response.status_code != 200:
            print(f"【錯誤】無法從手臂取得影像，HTTP 代碼: {response.status_code}")
            return False

        image_data = response.content  # 取得照片的二進位數據
        print(f"成功取得影像，大小: {len(image_data)} bytes")

        # 2. 準備加密 FTPS 上傳
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        remote_filename = f"tm_camera_{timestamp}.jpg"

        ftps = ReusedTLS_FTP()
        ftps.ssl_version = ssl.PROTOCOL_TLS_CLIENT
        ftps.context.check_hostname = False
        ftps.context.verify_mode = ssl.CERT_NONE

        print(f"正在連線至 FTPS 伺服器 {ftp_host}:{ftp_port}...")
        ftps.connect(ftp_host, ftp_port, timeout=10)
        ftps.login(user=ftp_user, passwd=ftp_password)
        ftps.prot_p()
        ftps.set_pasv(True)

        # 3. 灌入 FileZilla Server 倉庫
        # 注意：storbinary 需要 file-like object，需使用 io.BytesIO 包裝二進位資料
        with io.BytesIO(image_data) as img_stream:
            res = ftps.storbinary(f"STOR {remote_filename}", img_stream)
            print(f"伺服器回應: {res}")

        print(f"【備份成功】已直接截取相機畫面並存檔: {remote_filename}")
        ftps.quit()
        ftps = None
        return True

    except Exception as e:
        print(f"【執行失敗】原因: {e}")
        return False
    finally:
        if ftps is not None:
            try:
                ftps.close()
            except Exception:
                pass


if __name__ == "__main__":
    # 測試執行一次
    fetch_and_upload_image()
