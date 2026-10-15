"""VISCA: a position query waiting on a slow camera must not hold up pan/tilt commands."""

import socket
import threading
import time

import autotrack


def _slow_camera(reply_delay):
    """A TCP 'camera' that answers position queries after reply_delay s and logs what it got."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = []

    def serve():
        conn, _ = srv.accept()
        buf = b""
        while True:
            try:
                chunk = conn.recv(256)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\xff" in buf:
                msg, buf = buf.split(b"\xff", 1)
                got.append((time.monotonic(), msg))
                if msg == bytes([0x81, 0x09, 0x06, 0x12]):
                    def answer():
                        time.sleep(reply_delay)
                        conn.sendall(bytes([0x90, 0x50, 0, 0, 1, 0, 0xF, 0xF, 0xF, 0xE, 0xFF]))
                    threading.Thread(target=answer, daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    return srv, got


def test_move_is_not_blocked_by_a_pending_position_query(settings):
    srv, got = _slow_camera(reply_delay=0.25)
    settings.camera_ip, settings.visca_port = "127.0.0.1", srv.getsockname()[1]
    v = autotrack.VISCAController()
    assert v.connect()
    result = {}
    q = threading.Thread(target=lambda: result.setdefault("pos", v.query_pan_tilt(timeout=0.5)))
    q.start()
    time.sleep(0.05)                       # query sent, waiting for the reply
    t0 = time.monotonic()
    assert v.move(5, 0)
    assert time.monotonic() - t0 < 0.05    # didn't wait for the reply
    q.join(1.0)
    assert result["pos"] == (0x10, -2)
    v.disconnect()
    srv.close()


def test_query_times_out_cleanly_and_socket_still_works(settings):
    srv, got = _slow_camera(reply_delay=5.0)
    settings.camera_ip, settings.visca_port = "127.0.0.1", srv.getsockname()[1]
    v = autotrack.VISCAController()
    assert v.connect()
    t0 = time.monotonic()
    assert v.query_pan_tilt(timeout=0.2) is None
    assert time.monotonic() - t0 < 0.4
    assert v.stop()
    time.sleep(0.05)
    assert got[-1][1][:4] == bytes([0x81, 0x01, 0x06, 0x01])
    v.disconnect()
    srv.close()
