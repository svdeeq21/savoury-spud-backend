import pytest
from app.services import whatsapp


class _FakeResponse:
    status_code = 200
    text = "OK"


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return _FakeResponse()


async def test_send_buttons_rejects_more_than_three():
    """WhatsApp's own hard limit — caught here rather than discovered as a rejected/malformed API call."""
    buttons = [{"id": f"b{i}", "title": f"Option {i}"} for i in range(4)]
    with pytest.raises(ValueError, match="maximum of 3"):
        await whatsapp.send_buttons("2348012345678", "Pick one", buttons)


async def test_send_list_rejects_more_than_ten_rows_total():
    """The limit is across ALL sections combined, not per section."""
    sections = [
        {"title": "Section A", "rows": [{"id": f"a{i}", "title": f"A{i}"} for i in range(6)]},
        {"title": "Section B", "rows": [{"id": f"b{i}", "title": f"B{i}"} for i in range(5)]},
    ]
    with pytest.raises(ValueError, match="maximum of 10"):
        await whatsapp.send_list("2348012345678", "Menu", "Choose one", "View", sections)


async def test_send_buttons_allows_exactly_three(monkeypatch):
    monkeypatch.setattr(whatsapp.httpx, "AsyncClient", _FakeAsyncClient)
    buttons = [{"id": f"b{i}", "title": f"Option {i}"} for i in range(3)]
    await whatsapp.send_buttons("2348012345678", "Pick one", buttons)  # should not raise


async def test_send_list_allows_exactly_ten_rows_total(monkeypatch):
    monkeypatch.setattr(whatsapp.httpx, "AsyncClient", _FakeAsyncClient)
    sections = [{"title": "Section A", "rows": [{"id": f"a{i}", "title": f"A{i}"} for i in range(10)]}]
    await whatsapp.send_list("2348012345678", "Menu", "Choose one", "View", sections)  # should not raise


async def test_send_buttons_builds_expected_payload_shape(monkeypatch):
    captured = {}

    class _CapturingClient(_FakeAsyncClient):
        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            return _FakeResponse()

    monkeypatch.setattr(whatsapp.httpx, "AsyncClient", _CapturingClient)
    await whatsapp.send_buttons("2348012345678", "Pick a size", [{"id": "m-regular", "title": "Regular"}])

    assert captured["json"]["number"] == "2348012345678"
    assert captured["json"]["buttons"] == [{"buttonId": "m-regular", "buttonText": "Regular"}]
    assert "/message/sendButtons/" in captured["url"]
