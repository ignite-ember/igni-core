"""What the decisions service answers to a body it cannot read and a secret with non-ASCII text.

A body that fails validation is a 422 listing the errors, whatever
the failure (bad JSON, a wrong shape, a validator's refusal); a
secret is matched byte for byte, accents included.
"""

from __future__ import annotations

from igni_core.decisions_server.app import DecisionsApp
from tests.test_the_decisions_service_edges import AUTH, QUESTION, StubModel, client_for


class TestWhatItCannotRead:
    async def test_a_body_that_does_not_validate_is_a_422_listing_the_errors(self) -> None:
        async with client_for(DecisionsApp(StubModel(), "s3cret")) as http:
            missing = await http.post("/v1/predict", json={"state": {}}, headers=AUTH)
            garbage = await http.post("/v1/decide", content=b"{not json", headers=AUTH)
            empty = await http.post("/v1/decide", content=b"", headers=AUTH)
            wrong = await http.post("/v1/decide", json={"request": 3}, headers=AUTH)
        for reply in (missing, garbage, empty, wrong):
            assert reply.status_code == 422
            assert isinstance(reply.json()["detail"], list) and reply.json()["detail"]
        assert {e["loc"][0] for e in missing.json()["detail"]} == {"questions", "model"}

    async def test_a_validator_s_refusal_is_a_422_too_and_the_body_is_not_echoed(self) -> None:
        twice = {"contexts": [{"text": "a"}], "questions": [QUESTION, QUESTION]}
        async with client_for(DecisionsApp(StubModel(), "s3cret")) as http:
            reply = await http.post("/v1/decide", json={"request": twice}, headers=AUTH)
        assert reply.status_code == 422
        [error] = reply.json()["detail"]
        assert "every question has its own id" in error["msg"]
        assert "input" not in error


class TestANonAsciiSecret:
    async def test_a_secret_with_non_ascii_text_is_matched(self) -> None:
        app = DecisionsApp(StubModel(), "sécret")
        async with client_for(app) as http:
            wrong = await http.get("/v1/health", headers={"Authorization": "Bearer secret"})
            right = await http.get(
                "/v1/health", headers={"Authorization": "Bearer sécret".encode()}
            )
        assert wrong.status_code == 401
        assert right.status_code == 200
