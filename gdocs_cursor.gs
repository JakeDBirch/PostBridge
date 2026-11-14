/**
 * PostBridge for Google Docs: pulls land where the cursor is.
 *
 * PostBridge itself cannot see the cursor (Google's Docs API has no such
 * thing), so SEND TO SCRIPT > AT THE CURSOR leaves the pull at the end of
 * the Doc under a named range, and this script, which runs inside the Doc
 * and can see the cursor, moves it there.  With the panel open that happens
 * by itself within a few seconds; the menu does it on demand.
 *
 * Installed once per Doc, by PostBridge (INSTALL IN THE DOC) or by hand:
 * Extensions > Apps Script, paste, save, reload.  Once a person has opened
 * the panel, it opens by itself for them whenever they open the Doc.
 *
 * @OnlyCurrentDoc
 */

var PB_PENDING = 'postbridge_pending_pull';   // gdocs.PENDING_RANGE
var PB_AUTO = 'pb_auto_panel';                // per person: open the panel with the Doc

function onOpen() {
  DocumentApp.getUi().createMenu('PostBridge')
      .addItem('Open panel (pulls land at the cursor)', 'pbShowPanel')
      .addItem('Put waiting pulls at the cursor', 'pbPlaceFromMenu')
      .addItem('Leave waiting pulls where they are', 'pbReleaseFromMenu')
      .addItem('Stop opening the panel by itself', 'pbNoAuto')
      .addToUi();
  // For someone who has opened the panel before.  Google decides whether a
  // script may do this as a Doc opens; if not, the menu is still there.
  try {
    if (PropertiesService.getUserProperties().getProperty(PB_AUTO) === '1') { pbShowPanel(); }
  } catch (e) {}
}

function pbNoAuto() {
  try { PropertiesService.getUserProperties().deleteProperty(PB_AUTO); } catch (e) {}
}

function pbShowPanel() {
  try { PropertiesService.getUserProperties().setProperty(PB_AUTO, '1'); } catch (e) {}
  DocumentApp.getUi().showSidebar(
      HtmlService.createHtmlOutput(PB_PANEL_HTML).setTitle('PostBridge'));
}

function pbPlaceFromMenu() {
  var r = pbPlacePending();
  var ui = DocumentApp.getUi();
  if (r.note) {
    ui.alert('PostBridge', r.note, ui.ButtonSet.OK);
  } else if (!r.placed.length) {
    ui.alert('PostBridge', 'No pulls are waiting.', ui.ButtonSet.OK);
  }
}

function pbReleaseFromMenu() {
  var n = pbRelease();
  var ui = DocumentApp.getUi();
  ui.alert('PostBridge', n ? n + ' pull(s) left where they are.' : 'No pulls are waiting.',
           ui.ButtonSet.OK);
}

/** Every tab's document part (a Doc without tabs is its own). */
function pbParts_(doc) {
  var out = [];
  function walk(tabs) {
    for (var i = 0; i < tabs.length; i++) {
      try { out.push(tabs[i].asDocumentTab()); } catch (e) {}
      walk(tabs[i].getChildTabs());
    }
  }
  if (doc.getTabs) { walk(doc.getTabs()); }
  if (!out.length) { out.push(doc); }
  return out;
}

function pbIsLine_(el) {
  var t = el.getType();
  return t === DocumentApp.ElementType.PARAGRAPH || t === DocumentApp.ElementType.LIST_ITEM;
}

/** An element as what it is, so its own methods are there. */
function pbCast_(el) {
  var T = DocumentApp.ElementType;
  switch (el.getType()) {
    case T.PARAGRAPH: return el.asParagraph();
    case T.LIST_ITEM: return el.asListItem();
    case T.BODY_SECTION: return el.asBody();
    case T.TABLE_CELL: return el.asTableCell();
    case T.HEADER_SECTION: return el.asHeaderSection();
    case T.FOOTER_SECTION: return el.asFooterSection();
    case T.FOOTNOTE_SECTION: return el.asFootnoteSection();
  }
  return el;
}

/** The paragraph (or list item) an element sits in, or null. */
function pbLineOf_(el) {
  while (el && !pbIsLine_(el)) { el = el.getParent(); }
  return el ? pbCast_(el) : null;
}

function pbText_(el) {
  return pbIsLine_(el) ? pbCast_(el).getText() : '';
}

function pbIsEmpty_(line) {
  if (line.getType() !== DocumentApp.ElementType.PARAGRAPH || line.getText() !== '') { return false; }
  var n = line.getNumChildren();
  return n === 0 || (n === 1 && line.getChild(0).getType() === DocumentApp.ElementType.TEXT);
}

/**
 * The pulls PostBridge left waiting, in the order they were sent:
 * [{range, body, first, last}], first..last being the pull's lines as
 * child indexes of body.  A range whose text is gone is dropped.
 */
function pbWaiting_(doc) {
  var out = [];
  var parts = pbParts_(doc);
  for (var p = 0; p < parts.length; p++) {
    var body = parts[p].getBody();
    var ranges = parts[p].getNamedRanges(PB_PENDING);
    for (var i = 0; i < ranges.length; i++) {
      var els = ranges[i].getRange().getRangeElements();
      var first = -1, last = -1;
      for (var k = 0; k < els.length; k++) {
        var line = pbLineOf_(els[k].getElement());
        if (!line) { continue; }
        var at;
        try { at = body.getChildIndex(line); } catch (e) { continue; }
        if (first < 0 || at < first) { first = at; }
        if (at > last) { last = at; }
      }
      if (first < 0) {
        ranges[i].remove();
      } else {
        out.push({range: ranges[i], body: body, part: p, first: first, last: last});
      }
    }
  }
  out.sort(function (a, b) { return a.part - b.part || a.first - b.first; });
  return out;
}

/** Where the next pull goes: the cursor, or the end of a selection. */
function pbSpot_(doc) {
  var cursor = doc.getCursor();
  if (cursor) { return pbLineOf_(cursor.getElement()); }
  var sel = doc.getSelection();
  if (sel) {
    var els = sel.getRangeElements();
    if (els.length) { return pbLineOf_(els[els.length - 1].getElement()); }
  }
  return null;
}

/**
 * Move every waiting pull to the cursor, in order, each below the last.
 * Returns {placed: [first line of each pull], waiting: n, note: why not}.
 */
function pbPlacePending() {
  var lock = LockService.getDocumentLock();
  if (!lock.tryLock(500)) { return {placed: [], waiting: 0, note: ''}; }
  try {
    var doc = DocumentApp.getActiveDocument();
    var waiting = pbWaiting_(doc);
    var placed = [];
    if (!waiting.length) { return {placed: placed, waiting: 0, note: ''}; }
    var spot = pbSpot_(doc);
    if (!spot) {
      return {placed: placed, waiting: waiting.length,
              note: 'Click in the Doc where the pull should go.'};
    }
    // Later pulls first, so the indexes of earlier ones stay true while
    // their lines are taken out; they are put in afterwards, in order.
    var pulls = [];
    for (var w = waiting.length - 1; w >= 0; w--) {
      var it = waiting[w];
      var lines = [];
      for (var c = it.first; c <= it.last; c++) { lines.push(pbText_(it.body.getChild(c))); }
      var spotAt = -1;
      try { spotAt = it.body.getChildIndex(spot); } catch (e) {}
      if (spotAt >= it.first && spotAt <= it.last) {
        // The cursor is in this pull: it is already where it should be.
        it.range.remove();
        placed.unshift(lines[0]);
        continue;
      }
      pbRemoveLines_(it.body, it.first, it.last);
      try { it.range.remove(); } catch (e) {}
      pulls.unshift(lines);
    }
    for (var n = 0; n < pulls.length; n++) {
      spot = pbInsertBelow_(spot, pulls[n]);
      placed.push(pulls[n][0]);
    }
    try { doc.setCursor(doc.newPosition(spot, spot.getNumChildren())); } catch (e) {}
    return {placed: placed, waiting: 0, note: ''};
  } finally {
    lock.releaseLock();
  }
}

/**
 * Take lines first..last out of body.  A section's last paragraph cannot be
 * removed, so it is emptied and folded into the one above it.
 */
function pbRemoveLines_(body, first, last) {
  for (var c = last; c >= first; c--) {
    var el = pbCast_(body.getChild(c));
    try {
      el.removeFromParent();
    } catch (e) {
      el.clear();
      try { if (c > 0) { el.merge(); } } catch (e2) {}
    }
  }
}

/**
 * Put lines at the spot: an empty line takes the pull itself, any other
 * line gets it below, after one blank line.  Returns the pull's last line.
 */
function pbInsertBelow_(spot, lines) {
  var parent = pbCast_(spot.getParent());
  var at = parent.getChildIndex(spot);
  var from = 0;
  var last = spot;
  if (pbIsEmpty_(spot)) {
    spot.setText(lines[0]);
    from = 1;
  } else {
    at += 1;
    last = parent.insertParagraph(at, '');
  }
  for (var i = from; i < lines.length; i++) {
    at += 1;
    last = parent.insertParagraph(at, lines[i]);
  }
  return last;
}

/** Stop waiting: the pulls stay where they are.  Returns how many. */
function pbRelease() {
  var doc = DocumentApp.getActiveDocument();
  var n = 0;
  var parts = pbParts_(doc);
  for (var p = 0; p < parts.length; p++) {
    var ranges = parts[p].getNamedRanges(PB_PENDING);
    for (var i = 0; i < ranges.length; i++) { ranges[i].remove(); n++; }
  }
  return n;
}

var PB_PANEL_HTML = [
  '<!DOCTYPE html><html><head><base target="_top"><style>',
  'body{font:13px/1.45 Arial,sans-serif;color:#202124;margin:14px}',
  'h1{font-size:14px;margin:0 0 6px}',
  '#state{margin:10px 0;padding:8px 10px;border-radius:6px;background:#e8f0fe}',
  '#state.wait{background:#fef7e0}#state.err{background:#fce8e6}',
  'ul{padding-left:18px;margin:6px 0}li{margin:2px 0;word-break:break-word}',
  '.sub{color:#5f6368;font-size:12px}',
  'button{margin-top:8px}',
  '</style></head><body>',
  '<h1>Pulls land at the cursor</h1>',
  '<div class="sub">Keep this panel open. Click in the Doc where the next pull should go, ',
  'then in PostBridge: SEND TO SCRIPT, AT THE CURSOR.</div>',
  '<div id="state">Watching for pulls…</div>',
  '<div class="sub">Placed since the panel opened:</div><ul id="log"></ul>',
  '<button id="release" style="display:none">Leave waiting pulls where they are</button>',
  '<script>',
  'var state=document.getElementById("state"),log=document.getElementById("log"),',
  '    release=document.getElementById("release"),busy=false,timer=null;',
  'function show(text,cls){state.textContent=text;state.className=cls||"";}',
  'function again(ms){clearTimeout(timer);timer=setTimeout(check,ms);}',
  'function check(){',
  '  if(busy){return;}busy=true;',
  '  google.script.run.withSuccessHandler(function(r){',
  '    busy=false;',
  '    for(var i=0;i<r.placed.length;i++){var li=document.createElement("li");',
  '      li.textContent=r.placed[i];log.appendChild(li);}',
  '    release.style.display=r.waiting?"":"none";',
  '    if(r.note){show(r.waiting+" pull(s) waiting. "+r.note,"wait");}',
  '    else{show("Watching for pulls…");}',
  '    again(2500);',
  '  }).withFailureHandler(function(e){',
  '    busy=false;show("Could not reach the Doc: "+e.message,"err");again(8000);',
  '  }).pbPlacePending();',
  '}',
  'release.onclick=function(){google.script.run.withSuccessHandler(function(){',
  '  release.style.display="none";show("Watching for pulls…");}).pbRelease();};',
  'document.addEventListener("visibilitychange",function(){if(!document.hidden){again(0);}});',
  'check();',
  '</script></body></html>'
].join('\n');
