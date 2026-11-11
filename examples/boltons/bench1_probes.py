"""Silent-defect probes for the bench-1 features (doc §8, §12).

Written from the feature descriptions alone, BEFORE the LLM-planned run of
2026-11-11, without seeing any agent's code. They assert only behavior the goal
states or that follows from the named precedent (OrderedDict.move_to_end,
deque.rotate, Stats.get_quantile); anything the goal leaves open is not probed.

Run against a merged tree, in the sandbox (agent-written code is untrusted):
    cp examples/boltons/bench1_probes.py <exported tree>/
    python -m pytest -q bench1_probes.py
A failure is a defect that passed the worker's tests, the review and the merge suite.
"""

import itertools
import re
from collections import deque

import pytest
from boltons.cacheutils import LRI, LRU
from boltons.dictutils import OrderedMultiDict as OMD
from boltons.iterutils import interleave, interleave_iter
from boltons.listutils import BarrelList
from boltons.mathutils import inverse_lerp, lerp
from boltons.statsutils import Stats
from boltons.strutils import shorten
from boltons.urlutils import URL

# -- strutils.shorten: at most width chars, on a word boundary -----------------

TEXT = "the quick brown fox jumps over the lazy dog"


@pytest.mark.parametrize("width", range(len("...") + 4, len(TEXT) + 3))
def test_shorten_fits_and_breaks_on_a_word_boundary(width):
    out = shorten(TEXT, width)
    assert len(out) <= width
    if len(TEXT) <= width:
        assert out == TEXT
        return
    assert out.endswith("...")
    kept = out[: -len("...")].rstrip()
    assert TEXT.startswith(kept)
    # The cut is between words: what follows the kept text is a space.
    assert kept == "" or len(kept) == len(TEXT) or TEXT[len(kept)] == " "


def test_shorten_custom_placeholder():
    out = shorten(TEXT, 20, placeholder="~")
    assert len(out) <= 20 and out.endswith("~")


# -- iterutils.interleave / interleave_iter: round-robin until all exhausted ----


def test_interleave_round_robin_until_all_exhausted():
    assert list(interleave([1, 2, 3], "ab", [])) == [1, "a", 2, "b", 3]
    assert list(interleave()) == []


def test_interleave_iter_is_lazy():
    it = interleave_iter(itertools.count(), "ab")
    assert list(itertools.islice(it, 5)) == [0, "a", 1, "b", 2]


# -- mathutils.lerp / inverse_lerp -------------------------------------------


def test_lerp_endpoints_and_midpoint():
    assert lerp(2, 4, 0) == 2
    assert lerp(2, 4, 1) == pytest.approx(4)
    assert lerp(0, 10, 0.5) == pytest.approx(5)
    assert lerp(10, 0, 0.25) == pytest.approx(7.5)


def test_inverse_lerp_inverts_lerp():
    assert inverse_lerp(0, 10, 5) == pytest.approx(0.5)
    assert inverse_lerp(10, 0, 7.5) == pytest.approx(0.25)
    for t in (0, 0.3, 1):
        assert inverse_lerp(3, 9, lerp(3, 9, t)) == pytest.approx(t)


# -- cacheutils: LRI.resize (inherited by LRU) -------------------------------


def test_lri_shrink_evicts_least_recently_inserted():
    c = LRI(3)
    for k in "abc":
        c[k] = k
    c.resize(2)
    assert sorted(c) == ["b", "c"] and c.max_size == 2
    c["d"] = "d"
    assert len(c) == 2 and "d" in c


def test_lru_shrink_evicts_least_recently_used():
    c = LRU(3)
    for k in "abc":
        c[k] = k
    c["a"]  # a is now most recently used
    c.resize(2)
    assert sorted(c) == ["a", "c"]


def test_resize_grow_then_fill_and_stay_consistent():
    c = LRU(2)
    c["x"], c["y"] = 1, 2
    c.resize(4)
    for k in "pqr":
        c[k] = k
    assert len(c) == 4 and "x" not in c  # x was the oldest once 5 keys went in
    c.resize(1)
    assert list(c.keys()) == ["r"] and c["r"] == "r"
    c["s"] = "s"
    assert list(c.items()) == [("s", "s")]


# -- urlutils.URL.with_query_param: a NEW URL with key set to one value -------


def test_with_query_param_returns_new_consistent_url():
    u = URL("http://example.com/p?a=1&a=2&b=3")
    v = u.with_query_param("a", "9")
    assert v is not u
    assert u.to_text() == "http://example.com/p?a=1&a=2&b=3"  # original unchanged
    assert u.query_params.getlist("a") == ["1", "2"]
    assert v.query_params.getlist("a") == ["9"] and v.query_params["b"] == "3"
    # The text and the parsed params of the new URL agree (bench-3's defect class).
    assert URL(v.to_text()).query_params.getlist("a") == ["9"]


# Split out after the 2026-11-11 run (the only post-hoc change), to classify it.
# Root cause predates the change: in boltons, `qp = query_params` aliases a
# cachedproperty, and reading `url.qp` re-parses the URL's text and overwrites the
# cached `query_params`, discarding any in-place edit. A `with_query_param` that
# builds its result by editing `query_params` in place therefore returns a URL that
# reverts to the ORIGINAL once a caller reads `.qp`: a user-visible defect of the new
# method, inherited from that bug (building the result from new text avoids it).
def test_with_query_param_survives_reading_qp():
    v = URL("http://example.com/p?a=1&a=2&b=3").with_query_param("a", "9")
    assert v.qp.getlist("a") == ["9"]
    assert URL(v.to_text()).query_params.getlist("a") == ["9"]


def test_with_query_param_adds_a_missing_key():
    v = URL("http://example.com/").with_query_param("k", "v")
    assert re.search(r"[?&]k=v(&|$)", v.to_text())


# -- dictutils.OrderedMultiDict.move_to_end: every value of key moves ---------


def items():
    return OMD([("a", 1), ("b", 2), ("a", 3), ("c", 4)])


def test_move_to_end_moves_every_value():
    o = items()
    o.move_to_end("a")
    assert list(o.keys()) == ["b", "c", "a"]
    assert o.items(multi=True) == [("b", 2), ("c", 4), ("a", 1), ("a", 3)]
    assert o["a"] == 3 and o.getlist("a") == [1, 3]


def test_move_to_end_first():
    o = items()
    o.move_to_end("c", last=False)
    assert o.items(multi=True) == [("c", 4), ("a", 1), ("b", 2), ("a", 3)]


def test_move_to_end_missing_key_raises_and_dict_still_works():
    o = items()
    with pytest.raises(KeyError):
        o.move_to_end("zzz")
    o.move_to_end("b")
    o.add("b", 5)
    o.add("d", 6)
    assert o.items(multi=True) == [("a", 1), ("a", 3), ("c", 4), ("b", 2), ("b", 5),
                                   ("d", 6)]
    assert o.poplast("d") == 6 and o.getlist("b") == [2, 5]


# -- listutils.BarrelList.rotate: like collections.deque.rotate --------------


@pytest.mark.parametrize("size", [0, 1, 5, 2000])  # 2000 spans several barrels
@pytest.mark.parametrize("n", [0, 1, -1, 3, -7, 4001])
def test_barrellist_rotate_matches_deque(size, n):
    bl, dq = BarrelList(range(size)), deque(range(size))
    bl.rotate(n)
    dq.rotate(n)
    assert list(bl) == list(dq) and len(bl) == size


def test_barrellist_rotate_default_is_one():
    bl = BarrelList([1, 2, 3])
    bl.rotate()
    assert list(bl) == [3, 1, 2]


# -- statsutils.Stats.get_percentile: p in [0, 100] --------------------------


@pytest.mark.parametrize("p", [0, 10, 25, 50, 90, 100])
def test_get_percentile_is_get_quantile_scaled(p):
    s = Stats([5, 1, 9, 3, 7, 2, 8])
    assert s.get_percentile(p) == pytest.approx(s.get_quantile(p / 100.0))
