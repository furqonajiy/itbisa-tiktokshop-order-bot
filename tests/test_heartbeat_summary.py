"""Heartbeat summary: waiting-on-TikTok-Shop skips are reported separately
from real failures, with per-package reason lines."""

from src.telegram_sender import build_summary, _SUMMARY_DETAIL_MAX


def test_no_orders():
    assert build_summary("11:00", 0) == "✅ TikTok Shop - 11:00 - Tidak ada pesanan baru"


def test_all_sent():
    assert build_summary("12:00", 3) == "✅ TikTok Shop - 12:00 - 3 label terkirim"


def test_waiting_only_lists_packages_with_reason():
    text = build_summary("14:59", 1, waiting=[("1153...001", "resi belum siap")])
    lines = text.split("\n")
    assert lines[0] == "⚠️ TikTok Shop - 14:59 - 1 terkirim, 1 menunggu TikTok Shop (akan dicoba lagi)"
    assert "⏳ 1153...001 — resi belum siap" in lines
    assert "gagal" not in text


def test_mixed_waiting_and_failed():
    text = build_summary(
        "13:00", 2,
        waiting=[("P1", "resi belum siap")],
        failed=[("P2", "kirim Telegram gagal")],
    )
    lines = text.split("\n")
    assert lines[0] == "⚠️ TikTok Shop - 13:00 - 2 terkirim, 1 menunggu TikTok Shop, 1 gagal (akan dicoba lagi)"
    assert "⏳ P1 — resi belum siap" in lines
    assert "❌ P2 — kirim Telegram gagal" in lines


def test_detail_lines_capped_with_overflow():
    waiting = [(f"P{i:02d}", "resi belum siap") for i in range(15)]
    text = build_summary("10:00", 0, waiting)
    detail = [l for l in text.split("\n") if l.startswith("⏳")]
    assert len(detail) == _SUMMARY_DETAIL_MAX
    assert "...dan 5 lainnya" in text
