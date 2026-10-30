"""The second transcription pass -- Script -> Session's answer to Pull Quotes'
draft and refine.

Step 3 transcribes anything new with the tiny model, so the cut is there to
work on in a moment.  From then on this runs behind the screen: one file at a
time with the model the user chose, its words streamed over the draft's as
they are decoded (PqWorkflowMixin._pq_merge_streams, with the same throttle),
so the Assembly's transcript firms up from the top down while you work in it.

When a file is done its transcript is saved, labelled with the model that made
it, and everything that stands on it is matched again:

  * script pulls and VO blocks take their new match through Step 4's own
    merge (_bg_apply_batch).  A pull you accepted or reassigned keeps your
    cut; your marks and gaps stay on every pull.
  * a monolog is cut again once all its recordings are done.  Its pulls are
    numbered by the cut, so the new one replaces the old wholesale: at once
    when nothing has been touched, otherwise only when you say so (Update the
    cut), and then it keeps the pulls you worked on.

A draft is only ever a label on a saved transcript ("model": "tiny"), so a
session closed part way picks up where it left off the next time Step 4 opens.
"""
import os
import sys
import threading
import time

import engines
import monolog
from pq_workflow import PqWorkflowMixin
from utils import secs_tc, is_video

TICK_S = 1.2            # between redraws as words stream in, as Pull Quotes
RETRY_MS = 500          # a merge held back while the Assembly is in use waits this

# What the user set on a pull that a new match must not take with it.
_KEEP_KEYS = ("markers", "gap_after", "gap_after_s")


class _Live(object):
    """One file being refined: the words it had, and what the pass has heard."""

    def __init__(self, base, model, total):
        self.base = [w for w in (base or []) if not w.get("break")]
        self.heard = []
        self.model = model
        self.total = total
        self.frontier = 0.0
        self.version = 1
        self.final = None
        self._merged, self._merged_at = self.base, 1
        self.note = ""

    def add(self, new):
        self.heard.extend(new)
        self.frontier = max([self.frontier] + [float(w.get("end", 0.0)) for w in new])
        self.note = ""
        self.version += 1

    def words(self):
        if self.final is not None:
            return self.final
        if self._merged_at != self.version:
            self._merged = PqWorkflowMixin._pq_merge_streams(
                self.base, list(self.heard), [])
            self._merged_at = self.version
        return self._merged


class _Takes(object):
    """A VO part's recordings, for a session reopened at Step 4: no reconcile
    has set vo_bins up, but the pool still knows them."""

    def __init__(self, assets):
        self.paths = list(assets.get("audios") or assets.get("videos") or ())

    def get_paths(self):
        return list(self.paths)


class _State(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.thread = None
        self.jobs = []
        self.current = None
        self.live = {}            # path -> _Live
        self.done = {}            # path -> the model it was last refined with
        self.last_tick = 0.0
        self.tick_pending = False
        self.note = ""            # what the panel says when nothing is running
        self.held = None          # a monolog re-cut waiting for the go-ahead
        self.rebase = {}          # order -> new match, for the conform baseline
        self.script = None
        self.bins = {}            # the pool's VO parts, when vo_bins is empty


class RefineMixin(object):

    def _rf(self):
        st = self.__dict__.get("_rf_state")
        if st is None:
            st = self.__dict__["_rf_state"] = _State()
        return st

    def destroy(self):
        # The app is closing: the pass stops, and starts over next time.
        try:
            self._refine_stop()
        except Exception:
            pass
        super().destroy()

    # ── What the rest of the app asks ────────────────────────────────────
    def _refine_words(self, path):
        """The transcript of `path` as the pass has it (finished ones too),
        or None when no pass has touched it this session."""
        st = self._rf()
        with st.lock:
            live = st.live.get(path)
            return live.words() if live is not None else None

    def _refine_running(self):
        st = self._rf()
        with st.lock:
            return st.current is not None or bool(st.jobs)

    def _refine_held(self):
        return self._rf().held is not None

    def _refine_status(self):
        """One line for the Assembly's panel."""
        st = self._rf()
        with st.lock:
            job, more = st.current, len(st.jobs)
            live = st.live.get(job["path"]) if job else None
        if job is None:
            if st.held is not None:
                return ("Refined.  You have edited this cut, so it stays on "
                        "the draft's matches until you update it.")
            return st.note
        name = os.path.basename(job["path"])
        tail = "   ·   {} more".format(more) if more else ""
        if job["kind"] == "pulls":
            return "Refining {} pull{} of {}{}".format(
                len(job["pulls"]), "" if len(job["pulls"]) == 1 else "s", name, tail)
        if live is None:
            return "Refining {}{}".format(name, tail)
        if live.note:
            return "{}   ·   {}{}".format(live.note, name, tail)
        if live.total > 0:
            return "Refining with {}: {:.0f}%  ({} of {})   ·   {}{}".format(
                live.model, min(100.0, live.frontier * 100.0 / live.total),
                secs_tc(live.frontier).split(".")[0],
                secs_tc(live.total).split(".")[0], name, tail)
        return "Refining with {}   ·   {}{}".format(live.model, name, tail)

    def _refine_vo_bins(self):
        return getattr(self, "vo_bins", None) or self._rf().bins

    # ── Starting and stopping ────────────────────────────────────────────
    def _refine_resume(self):
        """Queue whatever still stands on a draft: at the end of Step 3, on
        coming to Step 4 (a session closed part way), after a model change."""
        if getattr(self, "_reconcile_busy", False):
            return
        if engines.draft_model() is None:
            return                       # tiny is the model chosen
        self._refine_queue(force=False)

    def _refine_again(self):
        """Transcribe the cut's files again with the model now chosen, Pull
        Quotes' RE-TRANSCRIBE: for after picking a better one."""
        if not getattr(self, "_reconcile_busy", False):
            self._refine_queue(force=True)

    def _refine_queue(self, force):
        rows = list(getattr(self, "results", None) or [])
        if not rows:
            return
        st = self._rf()
        script = getattr(self, "_script_path", None)
        if st.script != script:
            self._refine_stop()
            st.script = script
        st.bins = {}
        if not getattr(self, "vo_bins", None):
            try:
                st.bins = {pi: _Takes(d) for pi, d in self._pool.get_vo_assets().items()}
            except Exception:
                pass
        paths, windows = self._refine_sources(rows)
        threading.Thread(target=self._refine_scan, daemon=True,
                         args=(paths, windows, force, script)).start()

    def _refine_sources(self, rows):
        """Every file the cut's rows stand on, first needed first, and the
        rows matched from their own windows -- those have no file transcript
        to refine, only pulls to run again: {(token, file): [(model, pull)]}."""
        paths, windows = [], {}
        pulls = {p.get("order"): p for p in (getattr(self, "pulls", None) or [])}
        vo_paths = {}
        for pi, vb in self._refine_vo_bins().items():
            try:
                vo_paths[pi] = [ap for _vp, ap in engines.pair_takes(vb.get_paths())
                                if ap]
            except Exception:
                vo_paths[pi] = []
        monolog_row = bool(getattr(self, "_monolog", False))
        for r in sorted(rows, key=lambda x: x.get("order", 0)):
            if r.get("is_vo"):
                pi = r.get("part_index")
                found = vo_paths.get(pi) or []
                if monolog_row:
                    found = [p for v in vo_paths.values() for p in v]
                for p in found or [r.get("source_audio")]:
                    if p and p not in paths:
                        paths.append(p)
                continue
            src = r.get("source_audio")
            if not src or is_video(src):
                continue
            if r.get("_tx_model") and r.get("order") in pulls:
                windows.setdefault((r.get("token"), src), []).append(
                    (r["_tx_model"], pulls[r["order"]]))
            elif src not in paths:
                paths.append(src)
        return paths, windows

    def _refine_scan(self, paths, windows, force, script):
        """Worker: which of them want a pass."""
        target = engines.get_active_model_size()
        draft = engines.DRAFT_MODEL

        def wanted(model):
            return (model is not None and model != target) if force else model == draft
        jobs = [{"kind": "file", "path": p} for p in paths
                if wanted(engines.transcript_model(p))]
        for (token, src), items in windows.items():
            todo = [pull for model, pull in items if wanted(model)]
            if todo:
                jobs.append({"kind": "pulls", "path": src, "token": token,
                             "pulls": todo})
        self._ui(lambda: self._refine_enqueue(jobs, force, target, script))

    def _refine_enqueue(self, jobs, force, target, script):
        st = self._rf()
        if script != getattr(self, "_script_path", None):
            return
        try:
            pad = float(self.pad_var.get())
        except Exception:
            pad = engines.PAD_SECS
        with st.lock:
            taken = {(j["kind"], j["path"], j.get("token")) for j in st.jobs}
            if st.current:
                c = st.current
                taken.add((c["kind"], c["path"], c.get("token")))
            added = 0
            for j in jobs:
                key = (j["kind"], j["path"], j.get("token"))
                if key in taken or (not force and st.done.get(j["path"]) == target
                                    and j["kind"] == "file"):
                    continue
                if j["kind"] == "pulls":
                    j["pad"] = pad
                st.jobs.append(j)
                taken.add(key)
                added += 1
            if added:
                st.note = ""
                if st.thread is None:
                    cancel = st.cancel
                    st.thread = threading.Thread(target=self._refine_worker,
                                                 args=(cancel,), daemon=True)
                    st.thread.start()
            elif force and st.current is None and not st.jobs:
                st.note = "Everything in this cut is already transcribed with {}.".format(target)
        self._refine_show()

    def _refine_stop(self):
        """Drop the queue and stop the pass at its next segment.  Its draft
        is still saved, so a later _refine_resume starts it over."""
        st = self._rf()
        with st.lock:
            st.cancel.set()
            st.cancel = threading.Event()
            st.jobs = []
            st.current = None
            st.thread = None
            st.held = None
            st.note = ""
            st.done = {}
            stopped = [p for p, lv in st.live.items() if lv.final is None]
            for p in stopped:
                del st.live[p]
        for p in stopped:
            self._refine_show(p)
        self._refine_show()

    def _refine_model_changed(self):
        """A new model was picked: what is still a draft gets it."""
        if self._s4_is_live():
            self._refine_resume()

    # ── The worker ───────────────────────────────────────────────────────
    def _refine_worker(self, cancel):
        st = self._rf()
        me = threading.current_thread()
        while not cancel.is_set():
            with st.lock:
                if st.thread is not me:
                    return
                if not st.jobs:
                    st.current, st.thread = None, None
                    if not st.note and st.done:
                        st.note = "Transcript refined with {}.".format(
                            engines.get_active_model_size())
                    break
                job = st.current = st.jobs.pop(0)
            self._refine_show()
            try:
                if job["kind"] == "file":
                    self._refine_file(job, cancel)
                else:
                    self._refine_pulls(job, cancel)
            except engines.TranscriptionCancelled:
                return
            except Exception as exc:
                print("Refine failed on {}: {}".format(job["path"], exc),
                      file=sys.stderr)
                with st.lock:
                    st.live.pop(job["path"], None)
                    st.note = "Couldn't refine {}: {}".format(
                        os.path.basename(job["path"]), str(exc)[:160])
                self._refine_show(job["path"])
        self._refine_show()

    def _refine_file(self, job, cancel):
        st, path = self._rf(), job["path"]
        size = engines.get_active_model_size()
        try:
            total = float(engines.get_media_duration(path) or 0.0)
        except Exception:
            total = 0.0
        live = _Live(engines.cache_load(path) or engines.pb_transcript_load(path)[0],
                     size, total)
        if not engines._is_model_cached(size):
            live.note = "Downloading {}".format(size)
        with st.lock:
            st.live[path] = live
        self._refine_show()

        def _heard(new):
            with st.lock:
                live.add(new)
            self._refine_tick(path)

        words, blobs = engines.transcribe_file(
            path, model_size=size, segment_cb=_heard, cancel_event=cancel,
            blobs=engines.cache_load_blobs(path))
        if cancel.is_set():
            raise engines.TranscriptionCancelled()
        if not words:
            with st.lock:
                st.live.pop(path, None)
            return
        engines.cache_save(path, words, blobs=blobs, model=size)
        engines.pb_transcript_save(path, words, blobs=blobs, model=size)
        with st.lock:
            live.final = words
            st.done[path] = size
        self._refine_show(path)
        self._refine_rematch(path, cancel)

    def _refine_pulls(self, job, cancel):
        """Pulls matched from their own windows: run again, one at a time."""
        cursor = 0.0
        for pull in job["pulls"]:
            if cancel.is_set():
                raise engines.TranscriptionCancelled()
            start = pull.get("in_seconds", 0.0)
            at = cursor if start >= cursor - engines._PULL_ORDERING_LOOKBACK else 0.0
            # A result an earlier model left would answer for this one.
            engines.pull_result_cache_clear(pull, job["path"], job["pad"])
            r = engines.reconcile_interview_pull(
                pull, job["path"], pad=job["pad"], min_start_s=at)
            if r.get("status") in ("ok", "low_confidence", "snapped") \
                    and r.get("rec_out_s", 0.0) > 0.0:
                cursor = max(cursor, r["rec_out_s"])
            self._ui(lambda b=[r], p=job["path"]: self._refine_apply(b, p))
        st = self._rf()
        with st.lock:
            st.done[job["path"]] = engines.get_active_model_size()

    # ── Streaming to the screen ──────────────────────────────────────────
    def _refine_tick(self, path):
        st = self._rf()
        now = time.perf_counter()
        if now - st.last_tick < TICK_S or st.tick_pending:
            return
        st.last_tick, st.tick_pending = now, True
        self._ui(lambda: self._refine_show(path))

    def _refine_show(self, path=None):
        """Worker or main thread: tell the Assembly, on the main thread."""
        if threading.current_thread() is not threading.main_thread():
            self._ui(lambda: self._refine_show(path))
            return
        st = self._rf()
        if path is not None:
            st.tick_pending = False
        a = getattr(self, "_assembly", None)
        if a is None:
            return
        a.show_refine_status()
        if path is not None:
            a.transcript_changed(path)

    # ── Matching again ───────────────────────────────────────────────────
    def _refine_pending(self, paths):
        st = self._rf()
        with st.lock:
            return any(j["path"] in paths for j in st.jobs)

    def _refine_takes(self, pi):
        """A VO part's takes as the matchers want them, with today's words."""
        vb = self._refine_vo_bins().get(pi)
        if vb is None:
            return []
        known = {t[3]: t for t in
                 (getattr(self, "_vo_takes_by_part", None) or {}).get(pi, [])}
        offsets = {}
        for r in getattr(self, "results", None) or []:
            for td in r.get("takes_data") or []:
                if isinstance(td, dict) and td.get("apath"):
                    offsets[td["apath"]] = td.get("v_offset", 0.0)
        out = []
        for vp, ap in engines.pair_takes(vb.get_paths()):
            if not ap:
                continue
            words = (self._refine_words(ap) or engines.cache_load(ap)
                     or engines.pb_transcript_load(ap)[0])
            if not words:
                continue
            off = known[ap][1] if ap in known else offsets.get(ap, 0.0)
            out.append((words, off, vp, ap, engines.cache_load_blobs(ap)))
        return out

    def _refine_rematch(self, path, cancel):
        """Worker: new matches for whatever stands on `path`, once everything
        it is matched together with has its better words too."""
        batch = []
        takes_by_part = {}
        for pi, vb in self._refine_vo_bins().items():
            paths = [ap for _vp, ap in engines.pair_takes(vb.get_paths()) if ap]
            if path not in paths or self._refine_pending(paths):
                continue
            takes = self._refine_takes(pi)
            if not takes:
                continue
            takes_by_part[pi] = takes
            if getattr(self, "_monolog", False):
                fresh = monolog.reconcile(list(self.vo_blocks), takes)
                if not cancel.is_set():
                    self._ui(lambda f=fresh, t=takes_by_part:
                             self._refine_monolog_ready(f, t))
                return
            blocks = [b for b in (getattr(self, "vo_blocks", None) or [])
                      if b.get("part_index") == pi]
            batch.extend(engines.reconcile_vo_part(blocks, takes))
        rows = list(getattr(self, "results", None) or [])
        tokens = {r.get("token") for r in rows
                  if not r.get("is_vo") and r.get("source_audio") == path}
        if tokens:
            data = {"transcript": self._refine_words(path) or [],
                    "audio_cache": path, "media": [path],
                    "id": sorted(t for t in tokens if t)[0] if any(tokens) else "",
                    "_file_path": path}
            for pull in getattr(self, "pulls", None) or []:
                if pull.get("token") in tokens:
                    batch.append(engines.reconcile_pull_from_session(pull, data))
        if batch and not cancel.is_set():
            self._ui(lambda: self._refine_apply(batch, path, takes_by_part))

    def _refine_apply(self, batch, path=None, takes_by_part=None):
        """Main thread: fold new matches into the rows.  A new match can
        change where everything after it falls, so it waits while the
        Assembly is being played or dragged in; only the words move then."""
        a = getattr(self, "_assembly", None)
        if a is not None and a.busy():
            self.after(RETRY_MS, lambda: self._refine_apply(batch, path, takes_by_part))
            return
        if takes_by_part:
            self.__dict__.setdefault("_vo_takes_by_part", {}).update(takes_by_part)
        place = a.capture_place() if a is not None else None
        known = {r.get("order") for r in getattr(self, "results", None) or []}
        batch = [fresh for fresh in batch if fresh.get("order") in known]
        for fresh in batch:
            fresh["_refined"] = True
        self._bg_apply_batch(batch)
        st = self._rf()
        if st.rebase:
            rebase, st.rebase = st.rebase, {}
            self._conform_baseline_patch(rebase)
        if a is not None:
            a.refresh()
            a.restore_place(place)
            if path is not None:
                a.transcript_changed(path)

    def _refine_merge_row(self, cur, fresh):
        """Fold a refined match into its row (data only).  Returns "upgraded"
        (the row took it), "kept" (the user's cut stands), or None (nothing a
        card shows has changed)."""
        fresh.pop("_refined", None)
        order = fresh.get("order")
        if cur.get("_s4_reassigned") or (
                fresh.get("token") and cur.get("token")
                and fresh.get("token") != cur.get("token")):
            return "kept"
        self._rf().rebase[order] = dict(fresh)
        if cur.get("_s4_accepted"):
            # Your cut wins; the better words still belong to it.
            if fresh.get("words"):
                cur["words"] = fresh["words"]
            if "_tx_model" in fresh:
                cur["_tx_model"] = fresh["_tx_model"]
            if not self._segs_same(cur.get("segments"), fresh.get("segments")):
                _i, _o = fresh.get("rec_in_tc", ""), fresh.get("rec_out_tc", "")
                cur["_bg_note"] = ("kept your edit — refined match is {} → {}".format(_i, _o)
                                   if _i and _o else "kept your edit")
            return "kept"
        if cur.get("segments") and not fresh.get("segments"):
            cur["_bg_note"] = "the refined transcript found no match — kept the draft's"
            if "_tx_model" in fresh:
                cur["_tx_model"] = fresh["_tx_model"]
            return "kept"
        same = (self._segs_same(cur.get("segments"), fresh.get("segments"))
                and cur.get("status") == fresh.get("status"))
        keep = {k: cur[k] for k in tuple(self._S4_OWNED_KEYS) + _KEEP_KEYS if k in cur}
        cur.clear()
        cur.update(fresh)
        cur.update(keep)
        cur["_bg_matched"] = True
        return None if same else "upgraded"

    # ── A monolog's new cut ──────────────────────────────────────────────
    def _refine_row_touched(self, r):
        return bool(r.get("_s4_accepted") or r.get("_s4_ignored") or r.get("markers")
                    or r.get("seg_gaps") or r.get("gap_after_s", 0.0))

    def _refine_touched(self):
        """Has anything at all been done to this cut?"""
        if getattr(self, "_s4_undo_stack", None):
            return True
        if any(e["skip_var"].get() for e in getattr(self, "_rv", None) or []):
            return True
        return any(self._refine_row_touched(r) for r in self.results or [])

    def _refine_monolog_ready(self, fresh, takes_by_part):
        a = getattr(self, "_assembly", None)
        if a is not None and a.busy():
            self.after(RETRY_MS, lambda: self._refine_monolog_ready(fresh, takes_by_part))
            return
        self.__dict__.setdefault("_vo_takes_by_part", {}).update(takes_by_part)
        if self._refine_touched():
            self._rf().held = fresh
            self._refine_show()
            return
        self._refine_recut(fresh)

    def _refine_update_the_cut(self):
        """The Update the cut button: take the held re-cut."""
        fresh = self._rf().held
        if fresh is not None:
            self._refine_recut(fresh)

    def _refine_recut(self, fresh):
        """Main thread: the monolog's new cut in, around any pulls worked on.
        Its pulls are numbered anew, so Step 4's cards are built again and
        its undo history starts over."""
        self._rf().held = None
        for e in getattr(self, "_rv", None) or []:
            if e["skip_var"].get():
                e["res"]["_s4_ignored"] = True
        a = getattr(self, "_assembly", None)
        view = (a.pps, a.view0, a.playhead) if a is not None else None
        self.results = monolog.merge_recut(self.results or [], fresh,
                                           self._refine_row_touched)
        try:
            self._write_conform_baseline()
        except Exception:
            pass
        if self._s4_is_live():
            self._step4()
            if view is not None:
                self._refine_reopen(view)
        self._refine_show()

    def _refine_reopen(self, view, tries=0):
        """The Assembly goes with Step 4's old cards, and comes back once the
        new ones are built, a moment after the screen."""
        if getattr(self, "_s4_building", False) or not self._s4_is_live():
            if tries < 200:
                self.after(50, lambda: self._refine_reopen(view, tries + 1))
            return
        self._open_assembly()
        a = getattr(self, "_assembly", None)
        if a is not None:
            self.after(150, lambda: a.restore_view(*view))
