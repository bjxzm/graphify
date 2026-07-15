from __future__ import annotations

import socket
import ssl
import unittest
from unittest import mock

from graphify import security


def answer(host: str, port: int, *_args):
    addresses = {
        "127.0.0.1": "127.0.0.1",
        "localhost": "127.0.0.1",
        "proxy.internal": "10.0.0.5",
        "public.example": "93.184.216.34",
        "private.example": "10.1.2.3",
    }
    ip = addresses[host]
    family = socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, port or 0))]


class ProxyGuardTests(unittest.TestCase):
    def connection(self, host: str, target: str):
        return security._SSRFGuardedHTTPConnection(
            host,
            target_host=target,
            target_port=443,
        )

    def test_explicit_loopback_proxy_is_allowed_for_public_target(self):
        with (
            mock.patch.object(
                security.urllib.request,
                "getproxies",
                return_value={"http": "http://127.0.0.1:7890"},
            ),
            mock.patch.object(security.socket, "getaddrinfo", side_effect=answer),
        ):
            family, ip = self.connection(
                "127.0.0.1:7890", "public.example"
            )._guarded_endpoint()
        self.assertEqual(socket.AF_INET, family)
        self.assertEqual("127.0.0.1", ip)

    def test_alternate_loopback_proxy_host_and_port_are_allowed(self):
        with (
            mock.patch.object(
                security.urllib.request,
                "getproxies",
                return_value={"https": "http://localhost:8080"},
            ),
            mock.patch.object(security.socket, "getaddrinfo", side_effect=answer),
        ):
            family, ip = self.connection(
                "localhost:8080", "public.example"
            )._guarded_endpoint()
        self.assertEqual(socket.AF_INET, family)
        self.assertEqual("127.0.0.1", ip)

    def test_no_proxy_configuration_uses_public_destination_directly(self):
        with (
            mock.patch.object(
                security.urllib.request,
                "getproxies",
                return_value={},
            ),
            mock.patch.object(security.socket, "getaddrinfo", side_effect=answer),
        ):
            family, ip = self.connection(
                "public.example", "public.example"
            )._guarded_endpoint()
        self.assertEqual(socket.AF_INET, family)
        self.assertEqual("93.184.216.34", ip)

    def test_private_target_remains_blocked_through_local_proxy(self):
        with (
            mock.patch.object(
                security.urllib.request,
                "getproxies",
                return_value={"https": "http://127.0.0.1:7890"},
            ),
            mock.patch.object(security.socket, "getaddrinfo", side_effect=answer),
        ):
            with self.assertRaisesRegex(OSError, "SSRF blocked"):
                self.connection(
                    "127.0.0.1:7890", "private.example"
                )._guarded_endpoint()

    def test_unconfigured_loopback_endpoint_remains_blocked(self):
        with (
            mock.patch.object(
                security.urllib.request,
                "getproxies",
                return_value={},
            ),
            mock.patch.object(security.socket, "getaddrinfo", side_effect=answer),
        ):
            with self.assertRaisesRegex(OSError, "SSRF blocked"):
                self.connection(
                    "127.0.0.1:7890", "public.example"
                )._guarded_endpoint()

    def test_configured_non_loopback_private_proxy_remains_blocked(self):
        with (
            mock.patch.object(
                security.urllib.request,
                "getproxies",
                return_value={"http": "http://proxy.internal:8080"},
            ),
            mock.patch.object(security.socket, "getaddrinfo", side_effect=answer),
        ):
            with self.assertRaisesRegex(OSError, "non-loopback"):
                self.connection(
                    "proxy.internal:8080", "public.example"
                )._guarded_endpoint()

    def test_validate_url_still_blocks_loopback_targets(self):
        with mock.patch.object(security.socket, "getaddrinfo", side_effect=answer):
            with self.assertRaisesRegex(ValueError, "private/internal"):
                security.validate_url("http://127.0.0.1/")

    def test_https_proxy_uses_destination_for_sni(self):
        context = mock.Mock(spec=ssl.SSLContext)
        context.wrap_socket.return_value = object()
        connection = security._SSRFGuardedHTTPSConnection(
            "127.0.0.1:7890",
            target_host="public.example",
            target_port=443,
            context=context,
        )
        connection.set_tunnel("public.example", 443)
        fake_socket = object()
        with (
            mock.patch.object(
                connection,
                "_guarded_endpoint",
                return_value=(socket.AF_INET, "127.0.0.1"),
            ),
            mock.patch.object(
                security.socket,
                "create_connection",
                return_value=fake_socket,
            ),
            mock.patch.object(connection, "_tunnel"),
        ):
            connection.connect()
        context.wrap_socket.assert_called_once_with(
            fake_socket,
            server_hostname="public.example",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
