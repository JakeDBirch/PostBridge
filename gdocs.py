# -*- coding: utf-8 -*-
"""Google Docs bridge: the script stays in Docs, PostBridge reads and
annotates it there.

What this gives the episode:

* **Fetch** -- the Doc exported as plain text, stamped with its Drive
  version, so the script in the episode is never stale and a change since
  the last reconcile is known.
* **Inline notes** -- Script Check findings written into the Doc as
  ``// ⚠ PostBridge: ...`` lines right under the line they concern.  The
  script parser ignores ``//`` lines, so a note left behind can never break
  a reconcile.  Each note carries a named range, so the next pass finds
  its own notes and removes the ones whose problem is gone.  (The Docs API
  cannot make *suggestions*; these are ordinary edits, attributed to the
  account that signed in.)
* **Send to script** -- a pull block added from Pull Quotes, at the end of
  the section chosen (the end of the Doc, if it has no sections).
* **Fix format** -- Fix Format's structure repairs made in the Doc, line by
  line.  Wording and timecodes stay; Docs' version history can undo it.

Scope is ``drive.file`` only: PostBridge can reach the Docs a person picks
through Google's picker (or ones it created), never the rest of Drive.
Sign-in happens once per machine in the system browser; the token is kept
in the app-data folder.

Everything that talks to Google takes a *creds* object and is thin; the
planning of edits (``plan_notes``, ``findings_to_notes``, ``paragraphs``)
is pure and tested without a network.
"""
import difflib
import hashlib
import html
import http.server
import json
import os
import re
import threading
import urllib.parse
import webbrowser

from utils import app_state_dir

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
CLIENT_FILE = "google_client.json"
PICKER_FILE = "google_picker_key.txt"
TOKEN_FILE  = "google_token.json"

NOTE_PREFIX = "// ⚠ PostBridge: "
NOTE_RANGE  = "pbnote"       # named-range prefix on every note we write
NOTE_BG     = {"red": 1.0, "green": 0.86, "blue": 0.62}


# ── Configuration ─────────────────────────────────────────────────────────

def _candidates(name):
    here = os.path.dirname(os.path.abspath(__file__))
    return [os.path.join(app_state_dir(), name), os.path.join(here, name)]


def client_config_path():
    for p in _candidates(CLIENT_FILE):
        if os.path.isfile(p):
            return p
    return None


def picker_key():
    for p in _candidates(PICKER_FILE):
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return f.read().strip()
            except OSError:
                pass
    return ""


def token_path():
    return os.path.join(app_state_dir(), TOKEN_FILE)


def is_configured():
    return client_config_path() is not None


def project_number():
    """The Cloud project number, which the picker needs so a picked file is
    shared with the app under drive.file.  It leads the OAuth client id."""
    p = client_config_path()
    if not p:
        return ""
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        cid = (d.get("installed") or d.get("web") or {}).get("client_id", "")
        return cid.split("-", 1)[0] if "-" in cid else ""
    except Exception:
        return ""


def setup_message():
    return ("Google Docs is not set up on this machine.\n\n"
            "Put the OAuth client file as {} and the picker key as {} in\n"
            "{}\n\nThe README's 'Google Docs' section says how to get them."
            .format(CLIENT_FILE, PICKER_FILE, app_state_dir()))


# ── Sign-in ───────────────────────────────────────────────────────────────

def get_credentials(interactive=True):
    """Valid credentials, refreshing or signing in (browser) as needed.
    None when not configured, or when a sign-in would be needed and
    *interactive* is False."""
    if not is_configured():
        return None
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    creds = None
    tp = token_path()
    if os.path.isfile(tp):
        try:
            creds = Credentials.from_authorized_user_file(tp, SCOPES)
        except Exception:
            creds = None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except Exception:
            creds = None
    if not interactive:
        return None
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(client_config_path(), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent",
                                  success_message="PostBridge is signed in. You can close this tab.")
    _save_token(creds)
    return creds


def _save_token(creds):
    try:
        os.makedirs(app_state_dir(), exist_ok=True)
        with open(token_path(), "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    except OSError:
        pass


def sign_out():
    try:
        os.remove(token_path())
    except OSError:
        pass


def services(creds):
    from googleapiclient.discovery import build
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    docs  = build("docs",  "v1", credentials=creds, cache_discovery=False)
    return drive, docs


# ── Picker ────────────────────────────────────────────────────────────────

_PICKER_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>PostBridge: choose the script</title>
<style>body{font-family:Segoe UI,sans-serif;background:#191919;color:#f0ede8;padding:40px}
a{color:#e05c00}</style></head><body>
<h2>PostBridge</h2><p id="s">Opening the Google picker&hellip;</p>
<script>
var TOKEN=%(token)s, KEY=%(key)s, APPID=%(appid)s;
function done(msg){document.getElementById('s').textContent=msg;}
function cb(d){
  if(d.action==='picked'){var f=d.docs[0];
    fetch('/picked?'+new URLSearchParams({id:f.id,name:f.name}))
      .then(function(){done('Linked "'+f.name+'". You can close this tab.');});}
  else if(d.action==='cancel'){fetch('/cancel');done('Cancelled. You can close this tab.');}}
function init(){gapi.load('picker',function(){
  var v=new google.picker.DocsView(google.picker.ViewId.DOCUMENTS).setIncludeFolders(true);
  var p=new google.picker.PickerBuilder().setOAuthToken(TOKEN).setDeveloperKey(KEY)
    .addView(v).setCallback(cb).setTitle('Choose the episode script');
  if(APPID){p.setAppId(APPID);}
  p.build().setVisible(true);});}
</script><script src="https://apis.google.com/js/api.js" onload="init()"></script>
</body></html>"""


def pick_document(creds, timeout=300):
    """Open Google's picker in the system browser; return {"id", "name"}
    for the chosen Doc, or None on cancel / timeout."""
    key = picker_key()
    if not key:
        raise RuntimeError("No picker key: put it in " + PICKER_FILE)
    if not creds.valid:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
    page = (_PICKER_PAGE % {"token": json.dumps(creds.token), "key": json.dumps(key),
                            "appid": json.dumps(project_number())}).encode("utf-8")
    result = {}
    done = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/picked":
                q = urllib.parse.parse_qs(u.query)
                result["id"] = (q.get("id") or [""])[0]
                result["name"] = (q.get("name") or [""])[0]
                self._ok(b"ok"); done.set()
            elif u.path == "/cancel":
                self._ok(b"ok"); done.set()
            else:
                self._ok(page, "text/html; charset=utf-8")

        def _ok(self, body, ctype="text/plain"):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        webbrowser.open("http://127.0.0.1:{}/".format(srv.server_port))
        done.wait(timeout)
    finally:
        srv.shutdown()
    return result if result.get("id") else None


# ── Reading ───────────────────────────────────────────────────────────────

def normalize_text(raw):
    """Drive's plain-text export: UTF-8 with a BOM and CRLF line ends.
    Soft line breaks inside a paragraph come out as vertical tabs."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return raw.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").replace("\x0b", "\n")


def fetch_text(creds, doc_id, drive=None):
    """(text, meta) -- the Doc as script text and {"name", "modifiedTime",
    "version"}."""
    if drive is None:
        drive, _docs = services(creds)
    raw = drive.files().export(fileId=doc_id, mimeType="text/plain").execute()
    meta = drive.files().get(fileId=doc_id, fields="name,modifiedTime,version").execute()
    return normalize_text(raw), meta


def paragraphs(doc):
    """[(text, start, end)] for every paragraph of a Docs API document;
    *end* is past the paragraph's trailing newline, so inserting at *end*
    starts a new paragraph right below it."""
    out = []
    for el in doc.get("body", {}).get("content", []):
        p = el.get("paragraph")
        if not p:
            continue
        txt = "".join(r.get("textRun", {}).get("content", "") for r in p.get("elements", []))
        out.append((txt.rstrip("\n"), el.get("startIndex", 0), el.get("endIndex", 0)))
    return out


def existing_notes(doc):
    """[(name, start, end)] of the notes PostBridge wrote earlier."""
    out = []
    for name, grp in (doc.get("namedRanges") or {}).items():
        if not name.startswith(NOTE_RANGE):
            continue
        for nr in grp.get("namedRanges", []):
            for rg in nr.get("ranges", []):
                out.append((name, rg.get("startIndex", 0), rg.get("endIndex", 0)))
    return out


# ── Planning edits (pure) ─────────────────────────────────────────────────

def _anchor_by_name(finding, lines):
    """The script line a finding with no line number is about.  Most name a
    pull as [TOKEN in-out]; token-level ones (no session, weak transcript)
    name the token, and go under its first pull.  None for the rest."""
    from parsers import _HEADER_RE, _normalize_tc, _strip_comment
    by_pull, by_token = {}, {}
    for ln in lines:
        m = _HEADER_RE.match(_strip_comment(ln).strip())
        if m and m.group("tok"):
            key = (m.group("tok"), _normalize_tc(m.group("intc")), _normalize_tc(m.group("outtc")))
            by_pull.setdefault(key, ln)
            by_token.setdefault(m.group("tok"), ln)
    for m in re.finditer(r"\[([A-Z][A-Z0-9_]*) (\d{1,2}:\d{2}:\d{2})-(\d{1,2}:\d{2}:\d{2})\]",
                         finding.title + " " + str(finding.detail or "")):
        key = (m.group(1), _normalize_tc(m.group(2)), _normalize_tc(m.group(3)))
        if key in by_pull:
            return by_pull[key]
    for word in (re.findall(r"\b[A-Z][A-Z0-9_]*\b", finding.title)
                 + re.findall(r"\btoken '([A-Z][A-Z0-9_]*)'", str(finding.detail or ""))):
        if word in by_token:
            return by_token[word]
    return None


def findings_to_notes(findings, script_text):
    """[(anchor_line_text or None, message)] from Script Check findings.
    A finding is anchored to its line, else to the pull or token it names;
    one about the whole script goes to the top of the Doc."""
    lines = normalize_text(script_text).split("\n")
    notes = []
    for f in findings:
        msg = "{} {}: {}".format(f.severity.upper(), f.category, f.title)
        if f.detail:
            msg += " — " + " ".join(str(f.detail).split())
        if f.line and 1 <= f.line <= len(lines) and lines[f.line - 1].strip():
            anchor = lines[f.line - 1]
        else:
            anchor = _anchor_by_name(f, lines)
        notes.append((anchor, msg))
    return notes


def plan_delete_notes(doc):
    """Requests removing every PostBridge note in *doc*, highest first so
    earlier indexes stay valid."""
    reqs = []
    for _name, s, e in sorted(existing_notes(doc), key=lambda t: -t[1]):
        if e > s:
            reqs.append({"deleteContentRange": {"range": {"startIndex": s, "endIndex": e}}})
    return reqs


def plan_notes(doc, notes):
    """Requests inserting *notes* under their anchor lines in *doc* (which
    must hold no PostBridge notes).  Returns (requests, unplaced) where
    unplaced lists messages whose line could not be found; those go to the
    top of the Doc instead so nothing is silently dropped."""
    paras = paragraphs(doc)
    if not paras:
        return [], [m for _a, m in notes]
    norm = [" ".join(t.split()) for t, _s, _e in paras]
    inserts = []        # (index, message)
    unplaced = []
    used = set()
    for anchor, msg in notes:
        idx = None
        if anchor:
            want = " ".join(anchor.split())
            hits = [i for i, n in enumerate(norm) if n == want]
            if hits:
                # Repeated lines take a note each; one line can hold several.
                i = next((h for h in hits if h not in used), hits[0])
                used.add(i); idx = paras[i][2]
            else:
                unplaced.append(msg)
        if idx is None:
            idx = paras[0][2]
        inserts.append((idx, msg))
    reqs = []
    # Highest index first, so no insert shifts one still to come.  Notes at
    # the same index go in last-first, so they read in order in the Doc.
    for n, (idx, msg) in enumerate(sorted(reversed(inserts), key=lambda t: -t[0])):
        text = NOTE_PREFIX + msg + "\n"
        name = "{}:{}".format(NOTE_RANGE, hashlib.sha1(
            (msg + str(idx) + str(n)).encode("utf-8")).hexdigest()[:12])
        rng = {"startIndex": idx, "endIndex": idx + len(text)}
        reqs.append({"insertText": {"location": {"index": idx}, "text": text}})
        reqs.append({"updateTextStyle": {
            "range": rng,
            "textStyle": {"backgroundColor": {"color": {"rgbColor": NOTE_BG}},
                          "foregroundColor": {"color": {"rgbColor": {"red": 0.1, "green": 0.1, "blue": 0.1}}},
                          "bold": False, "italic": True},
            "fields": "backgroundColor,foregroundColor,bold,italic"}})
        reqs.append({"createNamedRange": {"name": name, "range": rng}})
    return reqs, unplaced


def plan_append(doc, text, after_marker=None):
    """Request appending *text* as new paragraphs: after the paragraph
    whose text equals *after_marker*, else at the end of the Doc."""
    body = text if text.endswith("\n") else text + "\n"
    if after_marker:
        want = " ".join(after_marker.split())
        for t, _s, e in paragraphs(doc):
            if " ".join(t.split()) == want:
                return [{"insertText": {"location": {"index": e}, "text": body}}]
    return [{"insertText": {"endOfSegmentLocation": {}, "text": "\n" + body.rstrip("\n")}}]


def script_lines(doc):
    """[(text, start, end)] for every line of the script in *doc*.  A soft
    line break counts as a line end, as it does in the plain-text export.
    Indexes count UTF-16 units and inline objects, so a paragraph whose
    length doesn't add up stays whole rather than risk a wrong index."""
    out = []
    for t, s, e in paragraphs(doc):
        if "\x0b" not in t or e - s != len(t) + 1:
            out.append((t, s, e))
            continue
        pos = s
        for part in t.split("\x0b"):
            out.append((part, pos, pos + len(part) + 1))
            pos += len(part) + 1
    return out


def plan_text_edits(doc, new_text):
    """Requests turning the script in *doc* into *new_text*, touching only
    the lines that differ, so formatting and comments elsewhere survive.
    Highest index first, so no edit shifts one still to come."""
    lines = script_lines(doc)
    old = [t.rstrip() for t, _s, _e in lines]
    new = [ln.rstrip() for ln in normalize_text(new_text).split("\n")]
    while old and not old[-1]:
        old.pop()
    while new and not new[-1]:
        new.pop()
    reqs = []
    ops = difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in reversed(ops):
        if tag == "equal":
            continue
        added = "\n".join(new[j1:j2])
        if i1 == len(lines):
            # Past the last line: a new paragraph inside the final newline.
            reqs.append({"insertText": {"location": {"index": lines[-1][2] - 1},
                                        "text": "\n" + added}})
            continue
        start = lines[i1][1]
        if i2 > i1:
            # The Doc's final newline can't be deleted.
            end = lines[i2 - 1][2] - (1 if i2 == len(lines) else 0)
            if end > start:
                reqs.append({"deleteContentRange": {"range": {"startIndex": start, "endIndex": end}}})
        body = added + ("" if i2 == len(lines) else "\n") if j2 > j1 else ""
        if body:
            reqs.append({"insertText": {"location": {"index": start}, "text": body}})
    return reqs


def _walk_doc(doc):
    """(lines, walked): the Doc's script lines, and script_normalize's reading
    of them.  Each walked line's ``n`` is 1 past its index in *lines*."""
    import script_normalize
    lines = script_lines(doc)
    return lines, script_normalize.walk("\n".join(t for t, _s, _e in lines))


def _sections(walked):
    """[(name, index in walked)]: [PART …] headers, and section lines like
    "Part 1: The Lake" in a draft not yet marked up.  A title under a header
    is that section, not another one: "Part 1: The Shots" under [PART Part 1],
    with a [VO] between them or not.  A section line right under a header is
    named as Fix Format would name the two, so it keeps that name afterwards."""
    import script_normalize
    from parsers import _HEADER_RE
    out = []
    for i, l in enumerate(walked):
        if l.kind not in ("part", "section"):
            continue
        name = _HEADER_RE.match(l.s).group("part").strip() if l.kind == "part" else l.s
        k, vo = i - 1, False
        while k >= 0 and (walked[k].kind == "vo" or walked[k].s.startswith(NOTE_PREFIX)):
            vo = vo or walked[k].kind == "vo"
            k -= 1
        under = bool(out) and k >= 0 and walked[k].kind == "part" and out[-1][1] == k
        if under and name.lower().startswith(out[-1][0].lower() + ":"):
            out[-1] = (name, k)
        elif under and l.kind == "section" and not vo:
            out[-1] = (out[-1][0] if name.lower() == out[-1][0].lower() else
                       _HEADER_RE.match(script_normalize._section_header(name, out[-1][0])).group("part"), k)
        else:
            out.append((name, i))
    return out


def doc_parts(doc):
    """The names of the sections of the script in *doc*, in order."""
    _lines, walked = _walk_doc(doc)
    return [name for name, _i in _sections(walked)]


def plan_append_to_part(doc, text, part):
    """Request adding *text* at the end of the section called *part*, or None
    if the Doc has no such section.  It goes inside the section's last line,
    before its newline, so it takes that line's style and not the next
    section's heading.  PostBridge's notes are passed over: inserting inside
    one would stretch its named range, and clearing the notes would then
    delete the pull.  A divider stays below it, with the section it closes."""
    lines, walked = _walk_doc(doc)
    heads = _sections(walked)
    for k, (name, i) in enumerate(heads):
        if name == part:
            stop = heads[k + 1][1] if k + 1 < len(heads) else len(walked)
            last = next(l for l in reversed(walked[i:stop])
                        if l.kind != "divider" and not l.s.startswith(NOTE_PREFIX))
            return [{"insertText": {"location": {"index": lines[last.n - 1][2] - 1},
                                    "text": "\n\n" + text.strip("\n")}}]
    return None


# ── Writing ───────────────────────────────────────────────────────────────

def _batch(docs, doc_id, reqs, revision_id=None):
    if not reqs:
        return
    body = {"requests": reqs}
    if revision_id:
        body["writeControl"] = {"requiredRevisionId": revision_id}
    docs.documents().batchUpdate(documentId=doc_id, body=body).execute()


def write_notes(creds, doc_id, notes, docs=None):
    """Replace PostBridge's notes in the Doc with *notes*.  Returns
    (placed, unplaced).  Each write is pinned to the revision it was
    planned against; if the Doc changed underneath, the plan is redone."""
    if docs is None:
        _drive, docs = services(creds)
    for attempt in range(3):
        doc = docs.documents().get(documentId=doc_id).execute()
        try:
            _batch(docs, doc_id, plan_delete_notes(doc), doc.get("revisionId"))
            doc = docs.documents().get(documentId=doc_id).execute()
            reqs, unplaced = plan_notes(doc, notes)
            _batch(docs, doc_id, reqs, doc.get("revisionId"))
            return len(notes) - len(unplaced), unplaced
        except Exception as e:
            if attempt == 2 or "revision" not in str(e).lower():
                raise
    return 0, [m for _a, m in notes]


def clear_notes(creds, doc_id, docs=None):
    if docs is None:
        _drive, docs = services(creds)
    doc = docs.documents().get(documentId=doc_id).execute()
    reqs = plan_delete_notes(doc)
    _batch(docs, doc_id, reqs, doc.get("revisionId"))
    return len(reqs)


def fix_format(creds, doc_id, title=None, docs=None):
    """Run Fix Format on the Doc itself.  Returns what it changed, in plain
    words.  PostBridge's notes go first: they describe the script as it was."""
    import script_normalize
    if docs is None:
        _drive, docs = services(creds)
    for attempt in range(3):
        doc = docs.documents().get(documentId=doc_id).execute()
        try:
            _batch(docs, doc_id, plan_delete_notes(doc), doc.get("revisionId"))
            doc = docs.documents().get(documentId=doc_id).execute()
            text = "\n".join(t for t, _s, _e in script_lines(doc))
            new, changes = script_normalize.normalize(text, title=title)
            if changes:
                _batch(docs, doc_id, plan_text_edits(doc, new), doc.get("revisionId"))
            return changes
        except Exception as e:
            if attempt == 2 or "revision" not in str(e).lower():
                raise
    return []


def append_block(creds, doc_id, text, after_marker=None, docs=None):
    if docs is None:
        _drive, docs = services(creds)
    doc = docs.documents().get(documentId=doc_id).execute()
    _batch(docs, doc_id, plan_append(doc, text, after_marker), doc.get("revisionId"))


def list_parts(creds, doc_id, docs=None):
    if docs is None:
        _drive, docs = services(creds)
    return doc_parts(docs.documents().get(documentId=doc_id).execute())


def append_to_part(creds, doc_id, text, part, docs=None):
    """Add a pull at the end of section *part*.  Each write is pinned to the
    revision it was planned against; if the Doc changed underneath, the plan
    is redone, because someone is likely writing in it."""
    if docs is None:
        _drive, docs = services(creds)
    for attempt in range(3):
        doc = docs.documents().get(documentId=doc_id).execute()
        reqs = plan_append_to_part(doc, text, part)
        if reqs is None:
            raise RuntimeError('The Doc no longer has a section called "{}".'.format(part))
        try:
            _batch(docs, doc_id, reqs, doc.get("revisionId"))
            return
        except Exception as e:
            if attempt == 2 or "revision" not in str(e).lower():
                raise


# ── Change detection ──────────────────────────────────────────────────────

def pull_keys(script_text):
    """{(token, in, out): body} for every pull in a script, for telling
    which pulls changed between two fetches."""
    from parsers import parse_script
    try:
        _t, _p, pulls, _v, _d, _w = parse_script(normalize_text(script_text))
    except Exception:
        return {}
    out = {}
    for p in pulls:
        key = (p.get("token"), p.get("in_tc") or p.get("in"), p.get("out_tc") or p.get("out"))
        out[key] = " ".join(str(p.get("quote_text", "")).split())
    return out


def diff_pulls(old_text, new_text):
    """(added, removed, changed) pull counts between two script versions."""
    a, b = pull_keys(old_text or ""), pull_keys(new_text or "")
    added   = len(set(b) - set(a))
    removed = len(set(a) - set(b))
    changed = sum(1 for k in set(a) & set(b) if a[k] != b[k])
    return added, removed, changed
