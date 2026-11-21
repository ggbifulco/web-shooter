from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
API_KEY = "secret"


class FakePetstore(BaseHTTPRequestHandler):
    """Tiny implementation of tests/fixtures/petstore3.yaml used for live-mode tests."""

    pets: dict[int, dict[str, object]]

    def log_message(self, *args: object) -> None:
        pass

    def _send(
        self, status: int, body: object | None = None, ctype: str = "application/json"
    ) -> None:
        data = (
            b""
            if body is None
            else (body if isinstance(body, bytes) else json.dumps(body).encode())
        )
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        if self.headers.get("X-API-Key") == API_KEY:
            return True
        self._send(401, {"error": "missing or wrong API key"})
        return False

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/openapi.yaml":
            return self._send(200, (FIXTURES / "petstore3.yaml").read_bytes(), "application/yaml")
        if url.path == "/v1/health":
            return self._send(200, {"status": "ok"})
        if not self._authorized():
            return None
        if url.path == "/v1/pets":
            q = parse_qs(url.query)
            page = int(q.get("page", ["1"])[0])
            pets = sorted(self.server.pets.values(), key=lambda p: p["id"])  # type: ignore[attr-defined]
            if "tag" in q:
                pets = [p for p in pets if p.get("tag") == q["tag"][0]]
            return self._send(200, {"data": pets[(page - 1) * 2 : page * 2]})
        if m := re.fullmatch(r"/v1/pets/(\d+)", url.path):
            pet = self.server.pets.get(int(m.group(1)))  # type: ignore[attr-defined]
            return self._send(200, pet) if pet else self._send(404, {"error": "not found"})
        return self._send(404, {"error": "no route"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        if urlparse(self.path).path != "/v1/pets":
            return self._send(404, {"error": "no route"})
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(body.get("name"), str):
            return self._send(422, {"error": "name is required"})
        pets = self.server.pets  # type: ignore[attr-defined]
        pet = {"id": max(pets, default=0) + 1, "name": body["name"], "tag": body.get("tag")}
        pets[pet["id"]] = pet
        return self._send(201, pet)

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        if m := re.fullmatch(r"/v1/pets/(\d+)", urlparse(self.path).path):
            self.server.pets.pop(int(m.group(1)), None)  # type: ignore[attr-defined]
            return self._send(204)
        return self._send(404, {"error": "no route"})


@pytest.fixture
def fake_api() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakePetstore)
    server.pets = {  # type: ignore[attr-defined]
        i: {"id": i, "name": f"pet{i}", "tag": "dog" if i % 2 else None} for i in range(1, 6)
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def petstore_path() -> str:
    return str(FIXTURES / "petstore3.yaml")


@pytest.fixture
def swagger_path() -> str:
    return str(FIXTURES / "swagger2.json")
