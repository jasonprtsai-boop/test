from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import app


class HttpServerTests(unittest.TestCase):
    @patch("app.create_server")
    def test_create_http_server_uses_waitress_with_explicit_limits(self, create_server):
        application = object()
        expected = object()
        create_server.return_value = expected

        server = app._create_http_server(application, "127.0.0.1", 8000, threads=8)

        self.assertIs(server, expected)
        create_server.assert_called_once_with(
            application,
            host="127.0.0.1",
            port=8000,
            threads=8,
            ident="TMChess",
            clear_untrusted_proxy_headers=True,
        )

    def test_close_http_server_closes_socket_then_workers(self):
        server = Mock()
        manager = Mock()
        manager.attach_mock(server.close, "close")
        manager.attach_mock(server.task_dispatcher.shutdown, "shutdown")

        app._close_http_server(server)

        self.assertEqual([call[0] for call in manager.mock_calls], ["close", "shutdown"])

    def test_close_failure_still_shuts_down_workers(self):
        server = Mock()
        server.close.side_effect = OSError("socket close failed")
        with self.assertRaises(OSError):
            app._close_http_server(server)
        server.task_dispatcher.shutdown.assert_called_once_with()

    @patch("builtins.print")
    @patch("app.RotatingFileHandler")
    @patch("app.logging.basicConfig")
    @patch("app._create_http_server")
    @patch("app.create_apps")
    def test_main_closes_service_and_both_servers(
        self,
        create_apps,
        create_http_server,
        _basic_config,
        _rotating_handler,
        _print,
    ):
        ui_app = Mock()
        vision_app = Mock()
        service = Mock()
        inbox = Mock()
        vision_server = Mock()
        ui_server = Mock()
        create_apps.return_value = (ui_app, vision_app, service, inbox)
        create_http_server.side_effect = (vision_server, ui_server)

        app.main()

        inbox.set_receiver_ready.assert_has_calls([unittest.mock.call(True), unittest.mock.call(False)])
        service.start_preflight_monitor.assert_called_once_with()
        service.close.assert_called_once_with()
        vision_server.run.assert_called_once_with()
        ui_server.run.assert_called_once_with()
        vision_server.close.assert_called_once_with()
        ui_server.close.assert_called_once_with()
        vision_server.task_dispatcher.shutdown.assert_called_once_with()
        ui_server.task_dispatcher.shutdown.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
