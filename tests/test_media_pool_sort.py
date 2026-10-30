"""Script -> Session Step 2: the TYPE and FILE headers sort the media pool.

Sorting is display only.  The pool's _rows stay alphabetical because
assignments, saves and a monolog's recording order all read them.

The Tk tests need tkinter and a display (CI runs them under xvfb); they
skip otherwise.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FILES = ["b_host.mp4", "c_host.wav", "a_guest.mov", "d_guest.wav"]


@pytest.fixture
def pool(monkeypatch):
    tk = pytest.importorskip("tkinter")
    import gui_components as GC
    monkeypatch.setattr(GC, "HAS_DND", False)
    monkeypatch.setattr(GC, "_get_dur_key", lambda path: None)
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    p = GC.MediaPool(root, [], [])
    p.pack(fill="both", expand=True)
    for fn in FILES:
        p._add(os.path.join(ROOT, "no_such_dir", fn))
    p._sort_rows()
    yield p
    root.update()
    root.destroy()


def _shown(pool):
    """Filenames top to bottom, as the table actually packs them."""
    by_widget = {r["row"]: r for r in pool._rows}
    return [os.path.basename(by_widget[w]["path"])
            for w in pool._table.pack_slaves()]


def _names(pool):
    return [os.path.basename(r["path"]) for r in pool._rows]


def _header(pool, text):
    """The header label reading *text*, with or without a sort arrow."""
    for cell in pool._col_hdr.winfo_children():
        for w in cell.winfo_children():
            if w.cget("text").split(" ")[0] == text:
                return w
    raise AssertionError("no {} header".format(text))


ALPHA = ["a_guest.mov", "b_host.mp4", "c_host.wav", "d_guest.wav"]


def test_rows_start_alphabetical(pool):
    assert _shown(pool) == ALPHA
    assert _header(pool, "FILE").cget("text") == "FILE ▲"


def test_type_groups_audio_then_video_with_names_in_order(pool):
    pool._sort_by("type")
    assert _shown(pool) == ["c_host.wav", "d_guest.wav",
                            "a_guest.mov", "b_host.mp4"]
    assert _header(pool, "TYPE").cget("text") == "TYPE ▲"
    assert _header(pool, "FILE").cget("text") == "FILE"


def test_second_click_puts_video_first_with_names_still_in_order(pool):
    pool._sort_by("type")
    pool._sort_by("type")
    assert _shown(pool) == ["a_guest.mov", "b_host.mp4",
                            "c_host.wav", "d_guest.wav"]
    assert _header(pool, "TYPE").cget("text") == "TYPE ▼"


def test_clicking_the_header_itself_sorts(pool):
    pool.update()
    _header(pool, "TYPE").event_generate("<ButtonRelease-1>")
    assert pool._sort_col == "type"
    assert _shown(pool)[0] == "c_host.wav"


def test_file_goes_back_to_names_and_reverses(pool):
    pool._sort_by("type")
    pool._sort_by("file")
    assert _shown(pool) == ALPHA
    pool._sort_by("file")
    assert _shown(pool) == list(reversed(ALPHA))


def test_type_header_is_as_wide_as_the_badge_it_sits_over(pool):
    pool._sort_by("type")
    pool.update()
    hdr = _header(pool, "TYPE")
    badge = pool._rows[0]["kind_lbl"]
    assert hdr.master.winfo_width() == 7 + badge.winfo_reqwidth()
    assert 6 + hdr.winfo_reqwidth() <= hdr.master.winfo_width()    # arrow not clipped


def test_sorting_never_changes_the_order_everything_else_reads(pool):
    pool._sort_by("type")
    assert _names(pool) == ALPHA
    pool._sort_by("file")
    pool._sort_by("file")
    assert _names(pool) == ALPHA


def test_a_file_added_while_sorted_lands_in_the_sorted_order(pool):
    pool._sort_by("type")
    pool._add(os.path.join(ROOT, "no_such_dir", "b_new.wav"))
    pool._sort_rows()
    assert _shown(pool) == ["b_new.wav", "c_host.wav", "d_guest.wav",
                            "a_guest.mov", "b_host.mp4"]
