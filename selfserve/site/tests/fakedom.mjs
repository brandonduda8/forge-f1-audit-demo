// Minimal fake DOM shared by the node tests (only what the render code uses).
export let focused = null;
// ---- minimal fake DOM (only what the render code uses) ----
export class FakeText {
  constructor(data) { this.nodeType = 3; this.data = String(data); this.parentNode = null; }
  get textContent() { return this.data; }
}
export class FakeElement {
  constructor(tag) {
    this.nodeType = 1; this.tagName = tag.toUpperCase(); this.children = [];
    this.attributes = new Map(); this.style = {}; this.parentNode = null; this.listeners = {};
  }
  get className() { return this.attributes.get("class") || ""; }
  set className(v) { this.attributes.set("class", String(v)); }
  get firstChild() { return this.children[0] || null; }
  appendChild(n) { n.parentNode = this; this.children.push(n); return n; }
  get hidden() { return this.attributes.has("hidden"); }
  set hidden(v) { if (v) this.attributes.set("hidden", ""); else this.attributes.delete("hidden"); }
  get disabled() { return this.attributes.has("disabled"); }
  set disabled(v) { if (v) this.attributes.set("disabled", ""); else this.attributes.delete("disabled"); }
  get id() { return this.attributes.get("id") || ""; }
  removeAttribute(k) { this.attributes.delete(k); }
  focus() { focused = this; }
  matches(sel) {
    let m;
    if ((m = /^\[([a-z-]+)(?:="([^"]*)")?\]$/.exec(sel))) return this.attributes.has(m[1]) && (m[2] === undefined || this.attributes.get(m[1]) === m[2]);
    return this.tagName === sel.toUpperCase();
  }
  querySelectorAll(sel) { const out = []; walk(this, (n) => { if (n !== this && n.nodeType === 1 && n.matches(sel)) out.push(n); }); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  click() { (this.listeners.click || []).forEach((fn) => fn({ preventDefault() {} })); }
  submit() { (this.listeners.submit || []).forEach((fn) => fn({ preventDefault() {} })); }
  removeChild(n) { this.children = this.children.filter((c) => c !== n); n.parentNode = null; return n; }
  setAttribute(k, v) { this.attributes.set(String(k), String(v)); }
  getAttribute(k) { return this.attributes.has(k) ? this.attributes.get(k) : null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  get textContent() { return this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this.children = []; if (v !== "") this.appendChild(new FakeText(v)); }
}
export const doc = {
  createElement: (t) => new FakeElement(t),
  createTextNode: (t) => new FakeText(t),
};


export function walk(n, fn) { fn(n); if (n.children) n.children.forEach((c) => walk(c, fn)); }
