"""get_waybill_pdf must separate 'still generating' from a real API failure.

It used to treat EVERY non-zero code as "not ready" and return None after three
attempts. An expired authorization, a permission error, an invalid package id,
a rate limit or a 5xx therefore sat in the queue forever while every heartbeat
said `resi belum siap` and the workflow stayed green.

`None` means waiting. Anything else raises, is caught per package in main.py,
and is reported as `resi gagal dibuat`.
"""

import pytest

from src import tiktokshop_client
from src.tiktokshop_client import _is_document_pending


class _Resp:
    def __init__(self, payload, status_code=200, text=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _install(monkeypatch, responses):
    """Feed get_waybill_pdf a fixed sequence of responses; no sleeping."""
    seq = list(responses)
    calls = []

    def fake_call(method, path, extra_query=None, body=None, include_cipher=True):
        calls.append(path)
        return seq.pop(0) if seq else seq_last

    seq_last = responses[-1]
    monkeypatch.setattr(tiktokshop_client, "_call_signed", fake_call)
    monkeypatch.setattr(tiktokshop_client.time, "sleep", lambda s: None)
    return calls


# ---------------------------------------------------------------------------
# The pure classifier
# ---------------------------------------------------------------------------
def test_pending_phrases_are_recognized():
    for msg in (
        "Shipping document is not ready",
        "document still generating",
        "Label generation IN PROGRESS",
        "processing, please try again later",
    ):
        assert _is_document_pending(12345, msg) is True, msg


def test_hard_error_messages_are_not_pending():
    for msg in (
        "Invalid access token",
        "Access denied",
        "package not found",
        "Rate limit exceeded",
        "Internal server error",
        "",
        None,
    ):
        assert _is_document_pending(99999, msg) is False, msg


# ---------------------------------------------------------------------------
# Pending path -> None
# ---------------------------------------------------------------------------
def test_pending_response_returns_none_after_retries(monkeypatch):
    pending = _Resp({"code": 12345, "message": "Shipping document is not ready"})
    calls = _install(monkeypatch, [pending, pending, pending])

    assert tiktokshop_client.get_waybill_pdf("PKG1") is None
    assert len(calls) == 3          # pending IS retried within the run


def test_pending_then_success_returns_pdf(monkeypatch):
    pending = _Resp({"code": 12345, "message": "still generating"})
    ok = _Resp({"code": 0, "data": {"doc_url": "https://x/y.pdf"}})
    _install(monkeypatch, [pending, ok])
    monkeypatch.setattr(tiktokshop_client, "_download_pdf", lambda u: b"%PDF-1.4")

    assert tiktokshop_client.get_waybill_pdf("PKG1") == b"%PDF-1.4"


# ---------------------------------------------------------------------------
# Hard errors -> raise, on the FIRST attempt
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "code,message",
    [
        (105002, "Invalid access token"),
        (105004, "Access denied for this shop"),
        (12000001, "package not found"),
        (429, "Rate limit exceeded"),
        (500, "Internal server error"),
    ],
)
def test_hard_api_errors_raise_immediately(monkeypatch, code, message):
    bad = _Resp({"code": code, "message": message, "request_id": "req-abc"})
    calls = _install(monkeypatch, [bad, bad, bad])

    with pytest.raises(RuntimeError) as e:
        tiktokshop_client.get_waybill_pdf("PKG1")

    # The operator needs all three to diagnose it without re-running.
    assert str(code) in str(e.value)
    assert message in str(e.value)
    assert "req-abc" in str(e.value)
    # An auth or validation failure will not fix itself in 10 seconds.
    assert len(calls) == 1


def test_non_200_raises(monkeypatch):
    _install(monkeypatch, [_Resp({}, status_code=502, text="<html>bad gateway</html>")])
    with pytest.raises(RuntimeError) as e:
        tiktokshop_client.get_waybill_pdf("PKG1")
    assert "502" in str(e.value)


def test_malformed_json_raises(monkeypatch):
    _install(monkeypatch, [_Resp(ValueError("Expecting value"), text="not json")])
    with pytest.raises(RuntimeError) as e:
        tiktokshop_client.get_waybill_pdf("PKG1")
    assert "not JSON" in str(e.value)


def test_missing_code_field_raises(monkeypatch):
    # A payload shaped nothing like the API contract must not KeyError.
    _install(monkeypatch, [_Resp({"unexpected": True})])
    with pytest.raises(RuntimeError) as e:
        tiktokshop_client.get_waybill_pdf("PKG1")
    assert "no code field" in str(e.value)


def test_empty_doc_url_is_treated_as_pending(monkeypatch):
    # code 0 but nothing to download yet — genuinely a wait, not a failure.
    blank = _Resp({"code": 0, "data": {"doc_url": "   "}})
    calls = _install(monkeypatch, [blank, blank, blank])
    assert tiktokshop_client.get_waybill_pdf("PKG1") is None
    assert len(calls) == 3


def test_missing_data_block_is_treated_as_pending(monkeypatch):
    # code 0 with no data block must not KeyError either.
    blank = _Resp({"code": 0})
    _install(monkeypatch, [blank, blank, blank])
    assert tiktokshop_client.get_waybill_pdf("PKG1") is None
