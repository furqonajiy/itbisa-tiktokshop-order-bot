"""Unit tests for caption formatting helpers (pure string logic)."""

from src.telegram_sender import _mono, build_caption


def test_mono_wraps_in_code_span():
    assert _mono("ITBISA-X") == "`ITBISA-X`"
    assert _mono(123) == "`123`"


def test_mono_strips_backticks_so_span_cannot_break():
    assert _mono("a`b`c") == "`abc`"


def test_build_caption_single_courier_wraps_copyable_values():
    order = {
        "id": "576412345678901234",
        "line_items": [
            {"seller_sku": "ITBISA-BLUETOOTH-MODULE-HC05", "shipping_provider_name": "SPX Hemat"},
        ],
    }
    caption = build_caption(order)
    assert "`576412345678901234`" in caption  # order id tap-to-copy
    assert "`SPX Hemat`" in caption  # courier tap-to-copy
    assert "• 1 x `ITBISA-BLUETOOTH-MODULE-HC05`" in caption


def test_build_caption_groups_quantity_per_sku():
    order = {
        "id": "1",
        "line_items": [
            {"seller_sku": "ITBISA-A", "shipping_provider_name": "SPX"},
            {"seller_sku": "ITBISA-A", "shipping_provider_name": "SPX"},
        ],
    }
    assert "• 2 x `ITBISA-A`" in build_caption(order)


def test_build_caption_multi_courier_inlines_courier_per_sku():
    order = {
        "id": "999",
        "line_items": [
            {"seller_sku": "ITBISA-A", "shipping_provider_name": "SPX Hemat"},
            {"seller_sku": "ITBISA-B", "shipping_provider_name": "JNE"},
        ],
    }
    caption = build_caption(order)
    assert "• 1 x `ITBISA-A` (`SPX Hemat`)" in caption
    assert "• 1 x `ITBISA-B` (`JNE`)" in caption
