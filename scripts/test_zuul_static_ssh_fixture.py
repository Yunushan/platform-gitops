"""Disposable loopback SSH host-key endpoint for Zuul allocation tests.

This fixture deliberately refuses authentication and cannot execute jobs.
"""

from contextlib import contextmanager
import socket
import threading
import unittest

try:
    import paramiko
except ImportError:
    paramiko = None


@contextmanager
def host_key_endpoint():
    if paramiko is None:
        raise RuntimeError("Paramiko is required for the disposable SSH fixture")
    key = paramiko.RSAKey.generate(2048)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(0.2)
    stopped = threading.Event()
    errors = []

    def serve():
        while not stopped.is_set():
            try:
                client, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if address[0] != "127.0.0.1":
                client.close()
                errors.append("non-loopback peer")
                continue
            transport = paramiko.Transport(client)
            try:
                transport.add_server_key(key)
                transport.start_server(server=paramiko.ServerInterface())
                transport.accept(timeout=2)
            except (EOFError, OSError, paramiko.SSHException):
                pass
            finally:
                transport.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield {"host": "127.0.0.1", "port": listener.getsockname()[1],
               "host_key": f"{key.get_name()} {key.get_base64()}"}
    finally:
        stopped.set()
        listener.close()
        thread.join(timeout=10)
        if thread.is_alive() or errors:
            raise RuntimeError("Disposable SSH fixture failed to stop cleanly")


@unittest.skipUnless(paramiko, "Paramiko unavailable")
class StaticSSHFixtureTests(unittest.TestCase):
    def test_loopback_handshake_presents_approved_key_and_refuses_authentication(self):
        with host_key_endpoint() as endpoint:
            connection = socket.create_connection((endpoint["host"], endpoint["port"]), timeout=5)
            transport = paramiko.Transport(connection)
            try:
                transport.start_client(timeout=5)
                remote = transport.get_remote_server_key()
                self.assertEqual(f"{remote.get_name()} {remote.get_base64()}", endpoint["host_key"])
                with self.assertRaises(paramiko.AuthenticationException):
                    transport.auth_none("fixture")
            finally:
                transport.close()
        self.assertFalse(threading.current_thread().daemon)


if __name__ == "__main__":
    unittest.main()
