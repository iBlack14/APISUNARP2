import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import api


class ConsultaValidationTests(unittest.TestCase):
    def test_valid_dni_is_normalized(self):
        self.assertEqual(api.validar_dni(" 12345678 "), "12345678")

    def test_invalid_dni_is_rejected(self):
        for dni in ("1234567", "123456789", "12a45678", ""):
            with self.subTest(dni=dni), self.assertRaises(HTTPException):
                api.validar_dni(dni)

    def test_response_does_not_include_unmapped_columns(self):
        response = api.row_to_response(
            {
                "DNI": "12345678",
                "NOMBRES": "Ana",
                "AP_PAT": "Pérez",
                "AP_MAT": "López",
                "SEXO": "2",
                "UBIGEO_DIR": "Lima-Lima-Miraflores",
                "INTERNAL_COLUMN": "not returned",
            }
        )
        self.assertEqual(response["nombre_completo"], "Ana Pérez López")
        self.assertEqual(response["sexo"], "Femenino")
        self.assertNotIn("INTERNAL_COLUMN", response)

    def test_api_key_is_required(self):
        with patch.object(api, "API_KEY", "k" * 32):
            with self.assertRaises(HTTPException) as raised:
                api.require_api_key(None)
        self.assertEqual(raised.exception.status_code, 401)

    def test_database_connections_require_verified_tls(self):
        with (
            patch.object(api, "DATABASE_URL", "postgresql://example/db"),
            patch.object(api.psycopg, "connect") as connect,
        ):
            api.get_conn()
        connect.assert_called_once_with(
            "postgresql://example/db", connect_timeout=10, sslmode="disable"
        )

    def test_weak_api_key_configuration_fails_closed(self):
        with patch.object(api, "API_KEY", "too-short"):
            with self.assertRaises(HTTPException) as raised:
                api.require_api_key("too-short")
        self.assertEqual(raised.exception.status_code, 503)

    def test_authenticated_requests_are_rate_limited(self):
        with (
            patch.object(api, "API_KEY", "k" * 32),
            patch.object(api, "_rate_count", 0),
            patch.object(api, "_rate_window_started", time.monotonic()),
        ):
            for _ in range(api.RATE_LIMIT):
                api.require_api_key("k" * 32)
            with self.assertRaises(HTTPException) as raised:
                api.require_api_key("k" * 32)
        self.assertEqual(raised.exception.status_code, 429)
        self.assertIn("Retry-After", raised.exception.headers)

    def test_dni_is_not_exposed_in_a_get_route(self):
        self.assertNotIn("/reniec/consulta/{dni}", api.app.openapi()["paths"])


if __name__ == "__main__":
    unittest.main()
