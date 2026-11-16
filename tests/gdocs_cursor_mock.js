// Runs gdocs_cursor.gs against a stand-in for Google's DocumentApp, so its
// logic is checked without a Google account.  It cannot prove Google's own
// behaviour (what merge() or getCursor() really do); that needs a real Doc.
//
//   node gdocs_cursor_mock.js <path to .gs> '<json scenario>'
//   scenario: {lines: [...], pending: [[first, last], ...], cursor: index|null}
//   prints:   {lines: [...], result: {...}, cursor: index|null, ranges: n}
const fs = require('fs');
const vm = require('vm');

const T = {PARAGRAPH: 'P', LIST_ITEM: 'LI', BODY_SECTION: 'B', TEXT: 'T', TABLE_CELL: 'TC',
           HEADER_SECTION: 'H', FOOTER_SECTION: 'F', FOOTNOTE_SECTION: 'FN'};

class Para {
  constructor(parent, text) { this.parent = parent; this.text = text; }
  getType() { return T.PARAGRAPH; }
  asParagraph() { return this; }
  getText() { return this.text; }
  getParent() { return this.parent; }
  getNumChildren() { return this.text ? 1 : 0; }
  getChild() { return {getType: () => T.TEXT}; }
  setText(t) { this.text = t; }
  clear() { this.text = ''; }
  removeFromParent() {
    const k = this.parent.kids, i = k.indexOf(this);
    if (i === k.length - 1) { throw new Error("Can't remove the last paragraph in a document section."); }
    k.splice(i, 1);
  }
  merge() {
    const k = this.parent.kids, i = k.indexOf(this);
    k[i - 1].text += this.text;
    k.splice(i, 1);
  }
}

class Body {
  constructor() { this.kids = []; }
  getType() { return T.BODY_SECTION; }
  asBody() { return this; }
  getChild(i) { return this.kids[i]; }
  getChildIndex(el) {
    const i = this.kids.indexOf(el);
    if (i < 0) { throw new Error('not a child'); }
    return i;
  }
  insertParagraph(i, text) {
    const p = new Para(this, text);
    this.kids.splice(i, 0, p);
    return p;
  }
}

const sc = JSON.parse(process.argv[3]);
const body = new Body();
sc.lines.forEach((t, i) => body.insertParagraph(i, t));
let ranges = (sc.pending || []).map(([a, b]) => {
  const els = [body.kids[a], body.kids[b]];
  const r = {
    getRange: () => ({getRangeElements: () => els.filter(e => body.kids.includes(e))
                                                 .map(e => ({getElement: () => e}))}),
    remove: () => { ranges = ranges.filter(x => x !== r); },
  };
  return r;
});
let cursor = sc.cursor == null ? null : body.kids[sc.cursor];

const doc = {
  getBody: () => body,
  getNamedRanges: () => ranges.slice(),
  getCursor: () => (cursor ? {getElement: () => cursor} : null),
  getSelection: () => null,
  newPosition: (el, off) => ({el, off}),
  setCursor: pos => { cursor = pos.el; },
};
// scenario.open: {run: ['pbShowPanel', 'open', ...], user: {...}, doc: {...}}
// prints {menu: [item titles], shown: n, triggers: n, user, doc}.
const op = sc.open || {};
const store = o => ({
  getProperty: k => (k in o ? o[k] : null),
  setProperty: (k, v) => { o[k] = v; },
  deleteProperty: k => { delete o[k]; },
});
const userProps = op.user || {}, docProps = op.doc || {};
const menu = [];
let shown = 0;
const menuObj = {
  addItem: t => { menu.push(t); return menuObj; },
  addSeparator: () => menuObj,
  addToUi: () => {},
};
const ui = {
  createMenu: () => menuObj,
  showSidebar: () => { if (op.blocked) { throw new Error(op.blocked); } shown++; },
};
const html = {setTitle: () => html};
let triggers = [];
const ctx = {
  DocumentApp: {ElementType: T, getActiveDocument: () => doc, getUi: () => ui},
  LockService: {getDocumentLock: () => ({tryLock: () => true, releaseLock: () => {}})},
  HtmlService: {createHtmlOutput: () => html},
  PropertiesService: {getUserProperties: () => store(userProps),
                      getDocumentProperties: () => store(docProps)},
  ScriptApp: {
    getUserTriggers: () => triggers.slice(),
    newTrigger: fn => {
      const b = {forDocument: () => b, onOpen: () => b, create: () => {
        if (op.noTriggers) { throw new Error(op.noTriggers); }
        const t = {getHandlerFunction: () => fn};
        triggers.push(t);
        return t;
      }};
      return b;
    },
    deleteTrigger: t => { triggers = triggers.filter(x => x !== t); },
  },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
if (sc.open) {
  // op.run: the functions to call, in order; a trigger made on the way
  // fires its handler at each 'open'.
  for (const step of op.run || ['open']) {
    if (step === 'open') {
      menu.length = 0;
      vm.runInContext('onOpen()', ctx);
      triggers.forEach(t => vm.runInContext(t.getHandlerFunction() + '()', ctx));
    } else if (step === 'block') {
      op.blocked = 'Not allowed';
    } else {
      vm.runInContext(step + '()', ctx);
    }
  }
  process.stdout.write(JSON.stringify({menu, shown, triggers: triggers.length,
                                       user: userProps, doc: docProps}));
  process.exit(0);
}
const result = vm.runInContext(sc.release ? '({released: pbRelease()})' : 'pbPlacePending()', ctx);
process.stdout.write(JSON.stringify({
  lines: body.kids.map(k => k.text), result,
  cursor: cursor ? body.kids.indexOf(cursor) : null, ranges: ranges.length}));
