import base64
from email.utils import formatdate
from hashlib import sha256
from typing import Generator
from urllib.parse import urlparse

from cryptography.hazmat.backends import default_backend as crypto_default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization as crypto_serialization
from cryptography.hazmat.primitives.asymmetric import padding
from httpx import Auth, Request, Response
from starlette.requests import HTTPConnection


class HttpSignatureAuth(Auth):
    DEFAULT_HEADERS = ["(request-target)", "host", "date"]
    POST_HEADERS = DEFAULT_HEADERS + ["digest"]

    def __init__(self, key_id: str, private_key: str):
        self._key_id = key_id
        self._private_key = crypto_serialization.load_pem_private_key(
            private_key.encode("utf-8"),
            password=None,
            backend=crypto_default_backend(),
        )

    @classmethod
    def _headers(cls, conn: HTTPConnection) -> list[str]:
        return cls.POST_HEADERS if conn.method == "POST" else cls.DEFAULT_HEADERS

    def construct_signature_data(self, conn: HTTPConnection) -> tuple[str, str]:
        headers = self._headers(conn)
        signature_data = []
        used_headers = []
        for header in headers:
            # FIXME support created and expires pseudo-headers
            if header.lower() == "(request-target)":
                method = (getattr(conn, "method", None) or conn.scope["method"]).lower()
                if hasattr(conn, "path_url"):
                    path = conn.path_url
                else:
                    path = conn.url.path
                signature_data.append(f"(request-target): {method} {path}")
                used_headers.append("(request-target)")
            elif header in conn.headers:
                name = header.lower()
                value = conn.headers[header]
                signature_data.append(f"{name}: {value}")
                used_headers.append(name)
            else:
                raise KeyError("Header %s not found", header)
        signature_text = "\n".join(signature_data)
        headers_text = " ".join(used_headers)
        return signature_text, headers_text

    def synthesize_headers(self, conn: HTTPConnection) -> None:
        for header in self._headers(conn):
            if header not in conn.headers:
                if header.lower() == "date":
                    conn.headers["Date"] = formatdate(
                        timeval=None, localtime=False, usegmt=True
                    )
                elif header.lower() == "digest" and conn.content is not None:
                    conn.headers["Digest"] = "SHA-256=" + base64.b64encode(
                        sha256(conn.content).digest()
                    ).decode("utf-8")
                elif header.lower() == "host":
                    conn.headers["Host"] = urlparse(conn.url).netloc

    def auth_flow(self, request: Request) -> Generator[Request, Response, None]:
        if not self._private_key:
            raise Exception("Private key unknown. Skipping signature.")

        self.synthesize_headers(request)
        signature_text, headers_text = self.construct_signature_data(request)

        signature = base64.b64encode(
            self._private_key.sign(
                signature_text.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256()
            )
        ).decode("utf-8")

        signature_fields = [
            f'keyId="{self._key_id}"',
            'algorithm="rsa-sha256"',
            f'headers="{headers_text}"',
            f'signature="{signature}"',
        ]

        signature_header = ",".join(signature_fields)

        request.headers["Signature"] = signature_header

        yield request
