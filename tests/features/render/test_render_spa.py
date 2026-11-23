"""is_spa: cases mirror the upstream project's render_page behaviour (shell + sparse text, builders, thin body)."""

from __future__ import annotations

from pathlib import Path

import pytest

from seoprobe.features.render.render import is_spa, visible_body_text

LONG = "Real paragraph content for visitors. " * 20  # ~740 chars: complete SSR page


def page(body: str, head: str = "") -> str:
    return f"<!doctype html><html><head><title>t</title>{head}</head><body>{body}</body></html>"


def test_empty_or_missing_is_spa() -> None:
    assert is_spa(None) is True
    assert is_spa("") is True


@pytest.mark.parametrize(
    "shell",
    [
        '<div id="root"></div><script src="/app.js"></script>',
        '<div id="__next"></div>',
        '<div id="app"></div>',
        '<div id="__nuxt"></div>',
        '<div data-svelte-h="x"></div>',
        "<astro-island uid=1></astro-island>",
        "<noscript>You need to enable JavaScript to run this app.</noscript><p>loading</p>",
        "<p>Please enable JavaScript</p>",
    ],
)
def test_empty_hydration_shells_are_spa(shell: str) -> None:
    assert is_spa(page(shell)) is True


def test_ssr_page_keeping_framework_markers_is_not_spa() -> None:
    """Next.js SSR keeps __next but ships full text: must NOT trigger a render."""
    assert is_spa(page(f'<div id="__next"><main>{LONG}</main></div>')) is False


def test_complete_server_rendered_page_is_not_spa() -> None:
    assert is_spa(page(f"<h1>Hello</h1><p>{LONG}</p>")) is False


def test_example_com_like_minimal_page_is_not_spa() -> None:
    """~125 chars of body text sits above the 100-char thin-body threshold."""
    body = (
        "<h1>Example Domain</h1><p>This domain is for use in illustrative examples in documents. "
        "You may use this domain in literature.</p>"
    )
    text = visible_body_text(page(body).lower())
    assert 100 <= len(text) < 400
    assert is_spa(page(body)) is False


def test_thin_body_without_any_shell_is_spa() -> None:
    assert is_spa(page("<p>hi</p>")) is True


def test_script_text_does_not_count_as_visible() -> None:
    body = "<script>" + "var x = 1; " * 200 + "</script><p>hi</p>"
    assert is_spa(page(body)) is True


def test_wix_single_marker_is_not_spa() -> None:
    html = page(
        "<p>" + "word " * 30 + "</p>",
        head='<meta name="generator" content="Wix.com Website Builder">',
    )
    assert len(visible_body_text(html.lower())) < 400
    assert is_spa(html) is False


def test_wix_two_markers_and_sparse_text_is_spa() -> None:
    html = page(
        "<p>" + "word " * 30 + "</p>",
        head='<meta content="Wix.com" name="generator"><script src="https://static.parastorage.com/x.js"></script>',
    )
    assert is_spa(html) is True


def test_wix_two_markers_but_full_text_is_not_spa() -> None:
    html = page(
        f"<p>{LONG}</p>",
        head='<meta content="Wix.com"><script src="https://static.parastorage.com/x.js"></script>',
    )
    assert is_spa(html) is False


def test_webflow_two_markers_sparse_is_spa() -> None:
    assert is_spa(page("<p>" + "word " * 30 + '</p><div data-wf-page="1" data-wf-site="2"></div>')) is True


def test_webflow_single_marker_not_spa() -> None:
    assert is_spa(page("<p>" + "word " * 30 + '</p><div data-wf-page="1"></div>')) is False


def test_squarespace_two_markers_sparse_is_spa() -> None:
    html = page(
        "<p>" + "word " * 30 + "</p>",
        head='<meta content="Squarespace"><link href="https://static1.squarespace.com/a.css">',
    )
    assert is_spa(html) is True


def test_markers_from_different_builders_do_not_combine() -> None:
    html = page("<p>" + "word " * 30 + '</p><div data-wf-page="1"></div><meta content="squarespace">')
    assert is_spa(html) is False


def test_visible_body_text_edge_cases() -> None:
    assert visible_body_text("<p>no body</p>") == ""
    assert visible_body_text("<body>a   b\n c</body>") == "a b c"
    assert visible_body_text("<body><style>p{}</style><template>x</template>kept</body>") == "kept"


@pytest.mark.parametrize(
    "name,expected",
    [("spa_shell.html", True), ("ssr_next.html", False), ("minimal_example.html", False)],
)
def test_golden_fixtures(fixtures: Path, name: str, expected: bool) -> None:
    html = (fixtures / "render" / name).read_text()
    assert is_spa(html) is expected
