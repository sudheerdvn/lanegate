"""Tests for lanegate/notify.py — shared ntfy.sh push helper."""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

from lanegate.notify import send_ntfy


def test_send_ntfy_returns_true_on_success():
    with patch("lanegate.notify.urllib.request.urlopen"):
        assert send_ntfy("some-topic", "hello") is True


def test_send_ntfy_returns_false_on_failure():
    with patch(
        "lanegate.notify.urllib.request.urlopen",
        side_effect=urllib.error.URLError("boom"),
    ):
        assert send_ntfy("some-topic", "hello") is False


def test_send_ntfy_posts_to_topic_url_with_title_header():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["title"] = req.get_header("Title")
        captured["data"] = req.data

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp()

    with patch("lanegate.notify.urllib.request.urlopen", side_effect=fake_urlopen):
        assert send_ntfy("my-topic", "hello world", title="custom-title") is True

    assert captured["url"] == "https://ntfy.sh/my-topic"
    assert captured["title"] == "custom-title"
    assert captured["data"] == b"hello world"
