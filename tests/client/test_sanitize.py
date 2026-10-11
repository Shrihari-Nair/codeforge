from aeos.client.sanitize import clean, safe


def test_ansi_and_osc_sequences_are_removed() -> None:
    evil = (
        "ok\x1b[31mRED\x1b[0m \x1b]0;pwned title\x07done \x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\"
    )
    assert clean(evil) == "okRED done link"


def test_control_characters_are_removed_but_newline_and_tab_kept() -> None:
    assert clean("a\x00b\x07c\x08d\te\nf\r\ng") == "abcd\te\nf\ng"


def test_rich_markup_is_not_interpreted() -> None:
    t = safe("[bold red]hello[/] [link=http://evil]x[/link]")
    assert t.plain == "[bold red]hello[/] [link=http://evil]x[/link]"
    assert not t.spans


def test_none_and_non_strings_become_text() -> None:
    assert clean(None) == "" and clean(42) == "42"
