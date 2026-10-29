"""The Pages site (site/index.html) stays self-contained and its links stay true."""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
HTML = (ROOT / "site" / "index.html").read_text()


def test_site_loads_nothing_from_third_parties():
    assert not re.search(r"<script[^>]+src=", HTML)
    assert not re.search(r"<link[^>]+stylesheet[^>]+href=\"https?:", HTML)


def test_links_into_the_repo_point_at_files_that_exist():
    for path in re.findall(r"github\.com/sc0Vu/SEAM/blob/main/([^\"#]+)", HTML):
        assert (ROOT / path).exists(), path


def test_in_page_anchors_exist():
    ids = set(re.findall(r'id="([^"]+)"', HTML))
    assert set(re.findall(r'href="#([^"]+)"', HTML)) <= ids
