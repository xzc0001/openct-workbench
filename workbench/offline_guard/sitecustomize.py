"""Applied to the reconstruction worker and its Python children, never the web server."""
import ipaddress
import socket


def permitted(host):
    if host in ("localhost", "localhost.localdomain", None):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_connect, _connect_ex, _resolve = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo


def connect(self, address):
    if isinstance(address, tuple) and not permitted(address[0]):
        raise OSError("Offline reconstruction: external network connection blocked")
    return _connect(self, address)


def connect_ex(self, address):
    if isinstance(address, tuple) and not permitted(address[0]):
        raise OSError("Offline reconstruction: external network connection blocked")
    return _connect_ex(self, address)


def resolve(host, *args, **kwargs):
    if not permitted(host):
        raise OSError("Offline reconstruction: external DNS lookup blocked")
    return _resolve(host, *args, **kwargs)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.getaddrinfo = resolve
socket.RECONSTRUCTION_OFFLINE = True
