"""Explicit-FTPS image upload for FileZilla Server."""

import os
import ssl
from datetime import datetime
from ftplib import FTP, FTP_TLS
from pathlib import Path
from typing import Optional


class ReusedTLSFTP(FTP_TLS):
    """FTP_TLS variant that reuses the control TLS session for data transfer."""

    def ntransfercmd(self, cmd, rest=None):
        connection, size = FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            session = self.sock.session if isinstance(self.sock, ssl.SSLSocket) else None
            connection = self.context.wrap_socket(
                connection,
                server_hostname=self.host,
                session=session,
            )
        return connection, size


def upload_image_via_ftps(
    local_file_path: os.PathLike[str] | str,
    remote_filename: Optional[str] = None,
    password: Optional[str] = None,
    host: str = "127.0.0.1",
    port: int = 21,
    username: str = "tm_user",
    verify_certificate: bool = False,
) -> str:
    """Upload one image using an encrypted passive FTPS data channel.

    The password is supplied by the caller and is never stored in project files.
    """
    local_path = Path(local_file_path)
    if not local_path.is_file():
        raise FileNotFoundError(f"找不到影像檔案：{local_path}")
    if not password:
        raise ValueError("請由呼叫端提供 FTPS password，不要寫入專案檔案。")
    if remote_filename is None:
        remote_filename = datetime.now().strftime("img_%Y%m%d_%H%M%S.jpg")
    if not remote_filename or "/" in remote_filename or "\\" in remote_filename:
        raise ValueError("remote_filename must be a file name, not a path")

    ftps = ReusedTLSFTP()
    connected = False
    if not verify_certificate:
        ftps.context.check_hostname = False
        ftps.context.verify_mode = ssl.CERT_NONE

    try:
        print("正在連線至 FTPS 伺服器...")
        ftps.connect(host, port, timeout=10)
        connected = True
        ftps.login(user=username, passwd=password)
        ftps.prot_p()
        ftps.set_pasv(True)
        print(f"正在傳輸影像至伺服器，儲存檔名為：{remote_filename}...")
        with local_path.open("rb") as image_file:
            response = ftps.storbinary(f"STOR {remote_filename}", image_file)
        if not response.startswith("226"):
            raise RuntimeError(f"FTP upload was not accepted: {response}")
        print("【成功】影像已順利傳輸並儲存！")
        ftps.quit()
        connected = False
        return response
    finally:
        if connected:
            ftps.close()


if __name__ == "__main__":
    raise SystemExit(
        "Import upload_image_via_ftps() and provide the password at runtime; "
        "do not put credentials in source files."
    )
