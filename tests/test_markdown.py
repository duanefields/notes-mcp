"""Markdown to the HTML Notes accepts.

The expectations here are not what a general Markdown renderer would produce.
They are what Notes was observed to do with each construct on a real Mac; where
the two disagree, Notes wins, because it is the thing that has to read the
output.
"""

import pytest

from notes_mcp.markdown import has_checklist_syntax, to_html


def test_a_plain_line_is_a_div():
    assert to_html("hello") == "<div>hello</div>"


def test_blank_lines_survive():
    assert to_html("one\n\ntwo") == "<div>one</div><div><br></div><div>two</div>"


def test_bullets():
    assert to_html("- one\n- two") == "<ul><li>one</li><li>two</li></ul>"


@pytest.mark.parametrize("marker", ["-", "*", "+"])
def test_every_bullet_marker_means_the_same_list(marker):
    assert to_html(f"{marker} item") == "<ul><li>item</li></ul>"


def test_numbered_lists():
    assert to_html("1. one\n2. two") == "<ol><li>one</li><li>two</li></ol>"


def test_nested_lists_nest():
    assert to_html("- top\n  - deep") == "<ul><li>top</li><ul><li>deep</li></ul></ul>"


def test_switching_list_kind_starts_a_new_list():
    assert to_html("- a\n1. b") == "<ul><li>a</li></ul><ol><li>b</li></ol>"


def test_a_paragraph_closes_an_open_list():
    assert to_html("- a\nprose") == "<ul><li>a</li></ul><div>prose</div>"


def test_inline_attributes():
    assert to_html("**b** *i* ~~s~~") == "<div><b>b</b> <i>i</i> <s>s</s></div>"


def test_bold_italic():
    assert to_html("***both***") == "<div><b><i>both</i></b></div>"


def test_underscore_forms():
    assert to_html("__b__ and _i_") == "<div><b>b</b> and <i>i</i></div>"


def test_intraword_underscores_are_left_alone():
    """``snake_case_name`` is an identifier, not italics."""
    assert to_html("snake_case_name") == "<div>snake_case_name</div>"


def test_a_lone_asterisk_is_not_italics():
    assert to_html("2 * 3 = 6") == "<div>2 * 3 = 6</div>"


def test_links():
    assert to_html("[docs](https://example.com)") == (
        '<div><a href="https://example.com">docs</a></div>'
    )


def test_a_query_string_is_not_double_escaped():
    """html.escape runs first, so escaping the href again yields &amp;amp;."""
    assert 'href="https://example.com/x?a=1&amp;b=2"' in to_html(
        "[x](https://example.com/x?a=1&b=2)"
    )


def test_html_in_the_text_is_escaped():
    assert to_html("a < b & c") == "<div>a &lt; b &amp; c</div>"


def test_a_quote_in_a_url_cannot_break_out_of_the_attribute():
    html = to_html('[x](https://example.com/")')
    assert '"><' not in html
    assert "&quot;" in html


def test_inline_code():
    assert to_html("run `x = 1`") == "<div>run <tt>x = 1</tt></div>"


def test_markers_inside_code_are_literal():
    assert to_html("`**not bold**`") == "<div><tt>**not bold**</tt></div>"


def test_fenced_blocks_are_monospaced():
    assert to_html("```\nx = 1\n```") == "<div><tt>x = 1</tt></div>"


def test_a_fence_escapes_its_contents():
    assert to_html("```\na < b\n```") == "<div><tt>a &lt; b</tt></div>"


def test_markers_inside_a_fence_are_literal():
    assert to_html("```\n- not a list\n```") == "<div><tt>- not a list</tt></div>"


def test_headings_become_bold():
    """Notes exposes no heading style to scripts; h1/h2/h3 all arrive bold."""
    assert to_html("# Title") == "<div><b>Title</b></div>"
    assert to_html("### Deep") == "<div><b>Deep</b></div>"


def test_checkboxes_become_plain_bullets():
    """No HTML spelling produces a real checklist. Five were tried."""
    assert to_html("- [ ] todo\n- [x] done") == "<ul><li>todo</li><li>done</li></ul>"


def test_checklist_syntax_is_detectable_so_it_can_be_reported():
    assert has_checklist_syntax("- [ ] todo")
    assert has_checklist_syntax("1. [x] done")
    assert not has_checklist_syntax("- ordinary bullet")
    assert not has_checklist_syntax("a [bracket] mid-line")


def test_carriage_returns_do_not_produce_empty_lines():
    assert to_html("one\r\ntwo") == "<div>one</div><div>two</div>"


def test_empty_input():
    assert to_html("") == "<div><br></div>"
