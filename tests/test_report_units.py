"""report.py building blocks that need no tools: escaping, shell quoting of the command log, the DoD ramp."""

from __future__ import annotations

import shlex

from argus_survey import report as rep

EVIL = '<script>alert(1)</script> & "x"'


def test_kv_and_table_escape_plain_values_and_keep_marked_html() -> None:
    doc = rep.kv([("name", EVIL), ("n", 3), ("none", None), ("link", rep.Html('<a href="#cmd-1">#1</a>'))])
    assert "<script>" not in doc and "&lt;script&gt;" in doc
    assert "<td>3</td>" in doc and "<td></td>" in doc
    assert '<a href="#cmd-1">#1</a>' in doc
    doc = rep.table(["File", "n"], [[EVIL, 1.5], [rep.Html("<code>a.laz</code>"), None]], {1})
    assert "<script>" not in doc and '<td class="num">1.5</td>' in doc and "<code>a.laz</code>" in doc


def test_already_escaped_values_are_not_escaped_twice() -> None:
    assert rep.kv([("k", rep.e("a & b"))]).count("&amp;amp;") == 0
    assert rep.e(rep.e("<b>")) == "&lt;b&gt;"
    # Html + plain text stays safe: the plain part is escaped, the Html part is not escaped again
    joined = rep.e("a & b") + ' <span class="x">'
    assert isinstance(joined, rep.Html) and joined == "a &amp; b &lt;span class=&quot;x&quot;&gt;"
    assert rep.e("1") + rep.Html("<br>") == "1<br>"
    # plain text + Html too: the plain part is escaped once and the result stays Html (not escaped again in a cell)
    left = "a & b" + rep.Html("<br>")
    assert isinstance(left, rep.Html) and left == "a &amp; b<br>"
    assert "<td>a &amp; b<br></td>" in rep.kv([("k", left)])


def test_figure_escapes_the_src_attribute() -> None:
    out = rep.figure('x" onerror="alert(1)', "cap")
    assert 'onerror="' not in out and "&quot;" in out
    assert rep.figure(None, "cap") == ""


def test_command_log_quotes_like_a_posix_shell() -> None:
    for arg in ("it's", "x'; rm -rf ~; '", "", "a`b`", "$HOME", "a\nb", "#c", "~", "plain", "a=b"):
        assert shlex.split(rep._quote(arg)) == [arg], arg
    assert rep._quote("plain") == "plain"


def test_coordinate_epoch_is_formatted() -> None:
    assert rep.e(2020.0) == "2020.0"


def test_dod_ramp_stops_increase_for_any_lod() -> None:
    for lod in (0.0, 0.05, 0.5, 1.0, 1.5, 3.0):
        ramp, sat = rep.dod_ramp(lod)
        values = [v for v, _ in ramp]
        assert values == sorted(values) and len(set(values)) == len(values), lod
        assert values[0] == -sat and values[-1] == sat and sat >= 1.0 and sat >= lod
    assert rep.dod_ramp(0.1)[1] == 1.0 and rep.dod_ramp(1.5)[1] == 3.0
    assert [v for v, _ in rep.dod_ramp(0.0)[0]] == [-1.0, 0.0, 1.0]
