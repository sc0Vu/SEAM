from reviewer.context import WHOLE_FILE_CHARS, FileChange, split_diff, view

PAD = "\n".join(f"def filler_{i}(x):\n    y = x * {i}\n    if y > 10:\n        y -= 1\n"
                f"    z = [y, x, {i}]\n    return sum(z) + {i}\n" for i in range(300))
SRC = f'''class cachedproperty:
    def __init__(self, func):
        self.func = func

    def __get__(self, obj, objtype=None):
        return self.func(obj)


{PAD}

class URL:
    def __init__(self, text):
        self._query = text

    @cachedproperty
    def query_params(self):
        return self._query

    qp = query_params

    def to_text(self):
        return self._query
'''


def _line(text, needle):
    return next(i for i, ln in enumerate(text.splitlines(), 1) if needle in ln)


def test_small_files_are_sent_whole():
    assert view("x = 1\n", FileChange(((1, 1),), frozenset())) == "x = 1\n"


def test_focused_view_has_outline_touched_code_used_names_and_aliases():
    assert len(SRC) > WHOLE_FILE_CHARS
    at = _line(SRC, "def to_text")  # a method added right after to_text
    out = view(SRC, FileChange(((at + 2, at + 2),), frozenset({"ret", "query_params"})))
    assert len(out) < len(SRC) / 3
    assert "def filler_200" in out  # outline lists everything...
    assert "sum(z) + 200" not in out  # ...but untouched bodies are elided
    assert "class URL:" in out and "self._query = text" in out  # class header + __init__
    assert "return self._query" in out and "@cachedproperty" in out  # used name, decorator
    assert "def __get__" in out  # one hop through the decorator
    assert "qp = query_params" in out  # an alias of a used name


def test_unparseable_files_fall_back_to_windows():
    text = "\n".join(f"line {i} (" for i in range(3000))
    out = view(text, FileChange(((1500, 1500),), frozenset()))
    assert "line 1500 (" in out and "line 1400 (" not in out and "not parseable" in out


def test_split_diff_reads_old_ranges_and_identifiers():
    diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -10,3 +10,4 @@ def f():\n"
            " ctx\n+    ret.query_params = kept\n-old_name()\n"
            "diff --git a/new.py b/new.py\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+z = 1\n")
    changes = split_diff(diff)
    assert set(changes) == {"x.py"}  # new files have no original to view
    assert changes["x.py"].hunks == ((10, 12),)
    assert {"ret", "query_params", "kept", "old_name"} <= changes["x.py"].names
