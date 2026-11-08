"""Direct SQL Server connection helper. DSN connections are intentionally omitted."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import pyodbc

@dataclass
class ConnectionSettings:
    server: str = ""
    database: str = "master"
    driver: str = "ODBC Driver 18 for SQL Server"
    authentication: str = "windows"
    username: str = ""
    password: str = ""
    trust_server_certificate: bool = True
    encrypt: str = "yes"
    timeout_seconds: int = 5

def _odbc_value(value: str) -> str:
    return "{" + str(value).replace("}", "}}") + "}"

def list_odbc_drivers() -> list[str]:
    return list(pyodbc.drivers())

class SQLServerConnection:
    """Manage one direct pyodbc connection; does not execute SQL statements."""
    def __init__(self, settings: ConnectionSettings):
        self.settings = settings
        self._connection: Optional[pyodbc.Connection] = None

    def _build_connection_string(self) -> str:
        s = self.settings
        auth = s.authentication.strip().lower()
        if not s.server.strip():
            raise ValueError("Enter a SQL Server name.")
        if not s.driver.strip():
            raise ValueError("Select an installed ODBC driver.")
        if auth not in {"windows", "sql"}:
            raise ValueError("authentication must be 'windows' or 'sql'.")
        if auth == "sql" and (not s.username or not s.password):
            raise ValueError("Username and password are required for SQL Server Authentication.")
        encrypt = s.encrypt.strip().lower()
        if encrypt not in {"yes", "no", "strict"}:
            raise ValueError("encrypt must be 'yes', 'no', or 'strict'.")
        parts = [
            f"DRIVER={_odbc_value(s.driver.strip())}",
            f"SERVER={_odbc_value(s.server.strip())}",
            f"DATABASE={_odbc_value(s.database.strip() or 'master')}",
            f"Encrypt={encrypt}",
            "TrustServerCertificate=" + ("yes" if s.trust_server_certificate else "no"),
        ]
        if auth == "windows":
            parts.append("Trusted_Connection=yes")
        else:
            parts.extend([f"UID={_odbc_value(s.username)}", f"PWD={_odbc_value(s.password)}"])
        return ";".join(parts) + ";"

    def connect(self) -> pyodbc.Connection:
        self.disconnect()
        self._connection = pyodbc.connect(
            self._build_connection_string(),
            timeout=max(1, int(self.settings.timeout_seconds)),
        )
        return self._connection

    def test_connection(self) -> tuple[bool, str]:
        try:
            connection = pyodbc.connect(
                self._build_connection_string(),
                timeout=max(1, int(self.settings.timeout_seconds)),
            )
            connection.close()
            return True, "Connection successful."
        except (pyodbc.Error, ValueError) as exc:
            return False, str(exc)

    def get_connection(self) -> pyodbc.Connection:
        if self._connection is None:
            raise ConnectionError("Not connected to SQL Server.")
        return self._connection

    def is_connected(self) -> bool:
        return self._connection is not None

    def disconnect(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None

    def __enter__(self) -> "SQLServerConnection":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.disconnect()
