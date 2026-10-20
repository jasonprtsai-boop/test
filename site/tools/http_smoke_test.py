"""Real loopback HTTP concurrency check with fake hardware only."""
import concurrent.futures
import json
import sys
import threading
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from waitress import create_server
from tests.fakes import make_service, wait_for
from tmchess.web import create_ui_app


def main():
    service, *_ = make_service([])
    server = None
    try:
        service.submit_start('player')
        wait_for(service, 'PLAYER_TURN')
        server = create_server(create_ui_app(service), host='127.0.0.1', port=0, threads=8, map={})
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.effective_port)
        def get_state(_):
            with urllib.request.urlopen(base + '/api/state', timeout=3) as response:
                return json.load(response)['phase']
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            assert all(phase == 'PLAYER_TURN' for phase in pool.map(get_state, range(32)))
        request = urllib.request.Request(base + '/api/game/stop', data=b'{}', headers={
            'Content-Type': 'application/json', 'Origin': 'http://localhost:8000'})
        with urllib.request.urlopen(request, timeout=3) as response:
            assert json.load(response)['phase'] == 'STOPPED'
        print('PASS: 32 real HTTP state queries / 16 concurrent clients; stop works; fake hardware only.')
    finally:
        if server is not None:
            server.close()
            server.task_dispatcher.shutdown()
            thread.join(timeout=3)
        service.close()


if __name__ == '__main__':
    main()
