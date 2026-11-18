# Copyright 2026 David
# SPDX-License-Identifier: GPL-2.0-or-later
"""
Run chain_paths through inkex, like Inkscape does, on the drawings in
test/ and on small cases, and check that chaining only joins: every
curve segment of the input is in the output (either direction, ends
moved by at most the snap distance) and nothing is added.

Needs inkex 1.4 (pip install --no-deps inkex==1.4.0, plus lxml, numpy,
tinycss2, cssselect, pyparsing, packaging), as in the GitHub workflow.
"""

import io
import os
import re
import sys
import tempfile

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import inkex

from chain_paths import ChainPaths

DRAWINGS = sorted(f for f in os.listdir(os.path.join(ROOT, "test")) if f.endswith(".svg"))
TOL = 0.05  # user units; the default snap distance is 0.01 mm


def path_ids(svg_text):
    return [
        re.search(r'\sid="([^"]*)"', p).group(1)
        for p in re.findall(r"<path\b[^>]*>", svg_text, re.S)
    ]


def run(svg_text, *args, ids=None):
    """Chain the given paths (all by default); returns (input, output)
    documents."""
    ids = path_ids(svg_text) if ids is None else ids
    out = io.BytesIO()
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "in.svg")
        with open(path, "w") as f:
            f.write(svg_text)
        ChainPaths().run([*args, *(f"--id={i}" for i in ids), path], output=out)
    before = inkex.load_svg(io.BytesIO(svg_text.encode())).getroot()
    # inkex writes nothing when the document did not change
    after = inkex.load_svg(io.BytesIO(out.getvalue() or svg_text.encode())).getroot()
    return before, after


def segments(svg):
    """All non-degenerate cubic segments of all paths, as 4x2 arrays."""
    out = []
    for node in svg.descendants().filter(inkex.PathElement):
        for sub in inkex.paths.CubicSuperPath(inkex.paths.Path(node.get("d"))):
            for a, b in zip(sub, sub[1:]):
                seg = np.array([a[1], a[2], b[0], b[1]], float)
                if np.abs(seg - seg[0]).max() > 1e-9:
                    out.append(seg)
    return out


def matched(segs, others):
    """For each of segs, whether others has it, forwards or backwards.
    A line drawn twice (there and back) may come out once: the
    extension drops the way back on purpose, so as not to cut twice."""
    return [
        any(np.abs(s - o).max() <= TOL or np.abs(s - o[::-1]).max() <= TOL for o in others)
        for s in segs
    ]


def svg_doc(*ds):
    paths = "".join(f'<path id="p{i}" d="{d}" style="fill:none;stroke:#000"/>' for i, d in enumerate(ds))
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm" viewBox="0 0 100 100">{paths}</svg>'


@pytest.mark.parametrize("name", DRAWINGS)
def test_drawings_keep_their_geometry(name):
    with open(os.path.join(ROOT, "test", name)) as f:
        text = f.read()
    before, after = run(text)
    lost = [s.round(2).tolist() for s, ok in zip(segments(before), matched(segments(before), segments(after))) if not ok]
    added = [s.round(2).tolist() for s, ok in zip(segments(after), matched(segments(after), segments(before))) if not ok]
    assert not lost, f"segments lost: {lost}"
    assert not added, f"segments added: {added}"


def test_pieces_become_one_path():
    # a square in four pieces, in scrambled order and directions
    before, after = run(svg_doc("M 0,0 L 10,0", "M 10,10 L 10,0", "M 0,10 L 10,10", "M 0,10 L 0,0"))
    paths = list(after.descendants().filter(inkex.PathElement))
    assert len(paths) == 1
    d = paths[0].get("d")
    assert d.count("M") == 1 and d.rstrip().lower().endswith("z")


def test_closing_keeps_the_last_curve():
    # two curves making a closed lens; the last must not become a line
    before, after = run(svg_doc("M 0,0 C 3,5 7,5 10,0", "M 10,0 C 7,-5 3,-5 0,0"))
    assert all(matched(segments(before), segments(after)))


def test_only_closed_chains_get_closed():
    # one path: a closed triangle in two pieces, and an open line apart
    before, after = run(svg_doc("M 0,0 L 10,0 L 5,8 M 50,50 L 60,50 M 5,8 L 0,0"))
    (path,) = after.descendants().filter(inkex.PathElement)
    subs = re.split(r"(?=[Mm])", path.get("d").strip())
    subs = [s for s in subs if s.strip()]
    closed = {("50" in s.split()[1] if len(s.split()) > 1 else False): s.rstrip().lower().endswith("z") for s in subs}
    assert len(subs) == 2
    assert closed == {False: True, True: False}, path.get("d")


def test_lens_in_one_path_keeps_both_curves():
    before, after = run(svg_doc("M 0,0 C 3,5 7,5 10,0 C 7,-5 3,-5 0,0"))
    assert all(matched(segments(before), segments(after)))


def test_there_and_back_is_cut_once():
    before, after = run(svg_doc("M 0,0 L 10,0 L 0,0"))
    assert len(segments(after)) == 1


def test_lone_points_do_not_crash():
    before, after = run(svg_doc("M 0,0 L 10,0 m 5,5 M 10,0 L 10,10"))
    assert all(matched(segments(before), segments(after)))


def test_snap_distance_in_mm_with_a_viewbox():
    # 1 user unit = 1 mm; ends 0.03 mm apart join at 0.05 mm, not at 0.01
    doc = svg_doc("M 0,0 L 10,0", "M 10.03,0 L 20,0")
    _, after = run(doc, "--epsilon=0.05")
    assert len(list(after.descendants().filter(inkex.PathElement))) == 1
    _, after = run(doc, "--epsilon=0.01")
    assert len(list(after.descendants().filter(inkex.PathElement))) == 2


def test_nothing_selected_means_every_path():
    _, after = run(svg_doc("M 0,0 L 10,0", "M 10,0 L 20,0"), ids=[])
    assert len(list(after.descendants().filter(inkex.PathElement))) == 1
