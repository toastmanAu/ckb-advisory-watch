"""send_message tests — respx-mocked Telegram Bot API."""
from __future__ import annotations

import httpx
import pytest
import respx

from agent.output.telegram import (
    PermanentSendError, TransientSendError, send_message,
)


API = "https://api.telegram.org"


@pytest.mark.asyncio
async def test_send_message_success_returns_message_id():
    with respx.mock() as mock:
        mock.post(f"{API}/botTOKEN/sendMessage").mock(
            return_value=httpx.Response(200, json={
                "ok": True,
                "result": {"message_id": 42, "chat": {"id": 123}},
            })
        )
        async with httpx.AsyncClient() as client:
            mid = await send_message(
                client, bot_token="TOKEN", chat_id="123",
                html_body="<b>hi</b>", inline_keyboard={"inline_keyboard": []},
            )
    assert mid == 42


@pytest.mark.asyncio
async def test_send_message_posts_correct_payload():
    captured: list[httpx.Request] = []
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    with respx.mock() as mock:
        mock.post(f"{API}/botTOKEN/sendMessage").mock(side_effect=handler)
        async with httpx.AsyncClient() as client:
            await send_message(
                client, bot_token="TOKEN", chat_id="-1001234",
                html_body="<b>body</b>",
                inline_keyboard={"inline_keyboard": [[{"text": "X", "url": "https://e"}]]},
            )
    import json
    body = json.loads(captured[0].content)
    assert body["chat_id"] == "-1001234"
    assert body["text"] == "<b>body</b>"
    assert body["parse_mode"] == "HTML"
    assert body["disable_web_page_preview"] is True
    assert body["reply_markup"] == {
        "inline_keyboard": [[{"text": "X", "url": "https://e"}]],
    }


@pytest.mark.asyncio
async def test_send_message_skips_reply_markup_when_no_buttons():
    captured: list[httpx.Request] = []
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    with respx.mock() as mock:
        mock.post(f"{API}/botTOKEN/sendMessage").mock(side_effect=handler)
        async with httpx.AsyncClient() as client:
            await send_message(
                client, bot_token="TOKEN", chat_id="1",
                html_body="body", inline_keyboard={"inline_keyboard": []},
            )
    import json
    body = json.loads(captured[0].content)
    assert "reply_markup" not in body


@pytest.mark.asyncio
async def test_send_message_429_raises_transient_with_retry_after():
    with respx.mock() as mock:
        mock.post(f"{API}/botTOKEN/sendMessage").mock(
            return_value=httpx.Response(429, json={
                "ok": False, "error_code": 429,
                "description": "Too Many Requests: retry after 7",
                "parameters": {"retry_after": 7},
            })
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(TransientSendError) as ei:
                await send_message(
                    client, bot_token="TOKEN", chat_id="1",
                    html_body="x", inline_keyboard={"inline_keyboard": []},
                )
    assert ei.value.retry_after == 7


@pytest.mark.asyncio
async def test_send_message_5xx_raises_transient_no_retry_after():
    with respx.mock() as mock:
        mock.post(f"{API}/botTOKEN/sendMessage").mock(
            return_value=httpx.Response(502, text="Bad Gateway")
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(TransientSendError) as ei:
                await send_message(
                    client, bot_token="TOKEN", chat_id="1",
                    html_body="x", inline_keyboard={"inline_keyboard": []},
                )
    assert ei.value.retry_after is None


@pytest.mark.asyncio
async def test_send_message_400_chat_not_found_raises_permanent():
    with respx.mock() as mock:
        mock.post(f"{API}/botTOKEN/sendMessage").mock(
            return_value=httpx.Response(400, json={
                "ok": False, "error_code": 400,
                "description": "Bad Request: chat not found",
            })
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(PermanentSendError) as ei:
                await send_message(
                    client, bot_token="TOKEN", chat_id="999",
                    html_body="x", inline_keyboard={"inline_keyboard": []},
                )
    assert "chat not found" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_send_message_400_bad_html_raises_permanent():
    with respx.mock() as mock:
        mock.post(f"{API}/botTOKEN/sendMessage").mock(
            return_value=httpx.Response(400, json={
                "ok": False, "error_code": 400,
                "description": "Bad Request: can't parse entities: Unexpected end tag",
            })
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(PermanentSendError):
                await send_message(
                    client, bot_token="TOKEN", chat_id="1",
                    html_body="<b>broken", inline_keyboard={"inline_keyboard": []},
                )


@pytest.mark.asyncio
async def test_send_message_network_error_raises_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed")
    with respx.mock() as mock:
        mock.post(f"{API}/botTOKEN/sendMessage").mock(side_effect=handler)
        async with httpx.AsyncClient() as client:
            with pytest.raises(TransientSendError):
                await send_message(
                    client, bot_token="TOKEN", chat_id="1",
                    html_body="x", inline_keyboard={"inline_keyboard": []},
                )
