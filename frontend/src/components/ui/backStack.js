// The phone's Back button closes the top overlay instead of leaving the page.
//
// Every overlay that opens pushes one history entry (same URL, the current state plus a
// depth marker); Back pops it and BackStack calls that overlay's close function. An
// overlay that closes itself (✕, backdrop, Escape, a finished action) calls release(),
// which steps history back over its own entry, so the history never collects dead
// entries. Pure history logic, no DOM: useBackClose.js wires it to React overlays;
// a copy lives in pokescan (src/pokescan/serve/static/live/backstack.js, the original).
//
// Routers: a pop that only changes the depth marker (same router key/idx) is ours, so it
// stops there (stopImmediatePropagation) and a router (React Router's BrowserRouter)
// never sees it. A pop that changes the route is left to the router; open overlays
// belong to the page left behind and are closed.
export const MARK = "__ov";
export const PENDING_MS = 1000;

export const depthOf = (s) => (s && Number.isInteger(s[MARK]) && s[MARK] > 0 ? s[MARK] : 0);
const routeOf = (s) => [s && s.key != null ? s.key : null, s && s.idx != null ? s.idx : null];
export const sameRoute = (a, b) => {
  const [ka, ia] = routeOf(a), [kb, ib] = routeOf(b);
  return ka === kb && ia === ib;
};
function strip(s) {
  if (!s || typeof s !== "object") return s;
  const { [MARK]: _drop, ...rest } = s;
  return Object.keys(rest).length ? rest : null;
}

export class BackStack {
  /** win: needs history (state, pushState, replaceState, go) and addEventListener("popstate"). */
  constructor(win = globalThis) {
    this.win = win;
    this.h = win.history;
    this.stack = [];       // {close, mark (0 = no entry yet), route}
    this.pending = 0;      // our own history.go() calls still to arrive as popstate
    this.expect = 0;       // the depth marker history lands on once they have
    this.returnTo = null;  // set while undoing a step back that crossed a navigation
    // a reload with an overlay open keeps the marker on this entry: drop it
    if (depthOf(this.h.state)) this.h.replaceState(strip(this.h.state), "");
    this.cur = this.h.state;
    // Know the current entry's state when a pop arrives, also after a router's own
    // pushState/replaceState (a pop is "ours" only if the route part did not change).
    // A router navigating from inside an open overlay (a link in it) replaces the
    // overlay's entry instead of stacking on it: Back from the new page then goes
    // straight to the page the overlay was on, and no dead overlay entry is left.
    const self = this, h = this.h;
    const replace = h.replaceState;
    for (const name of ["pushState", "replaceState"]) {
      const orig = h[name];
      if (typeof orig !== "function" || orig.__backstack) continue;
      const wrapped = function (...args) {
        const viaReplace = name === "pushState" && depthOf(h.state) > 0 && !sameRoute(args[0], h.state)
          && typeof replace === "function";
        const r = (viaReplace ? replace : orig).apply(h, args);
        self.cur = h.state;
        return r;
      };
      wrapped.__backstack = true;
      h[name] = wrapped;
    }
    this.onPop = this.onPop.bind(this);
    win.addEventListener("popstate", this.onPop);
  }

  get size() { return this.stack.length; }
  top() { return this.stack[this.stack.length - 1] || null; }
  isOpen(e) { return this.stack.includes(e); }
  maxMark() { return this.stack.reduce((m, e) => Math.max(m, e.mark), 0); }

  /** An overlay opened; close() is called when Back closes it. Returns its handle. */
  open(close) {
    const e = { close, mark: 0, route: null };
    this.stack.push(e);
    if (!this.pending) this.push(e);   // else: pushed once our history.go() has landed
    return e;
  }

  push(e) {
    const st = this.h.state;
    e.mark = depthOf(st) + 1;
    e.route = st;
    this.h.pushState({ ...(st && typeof st === "object" ? st : {}), [MARK]: e.mark }, "");
    this.cur = this.h.state;
  }

  /** The overlay closed itself: forget it and step back over its history entry. Idempotent. */
  release(e) {
    const i = this.stack.indexOf(e);
    if (i < 0) return;
    this.stack.splice(i, 1);
    if (!e.mark) return;
    const target = this.maxMark();
    // where history will be once our steps in flight have landed
    const m = this.pending ? this.expect : depthOf(this.h.state);
    // only on the page it was opened on (a link inside the overlay may have navigated)
    if (m > target && sameRoute(this.h.state, e.route)) this.go(target - m, target);
  }

  go(n, landing) {
    this.pending++;
    this.expect = landing;
    this.h.go(n);
    // a step that never lands (no such entry, a browser that drops it) must not keep new
    // overlays from getting their entry
    clearTimeout(this.pendT);
    this.pendT = setTimeout(() => {
      if (!this.pending) return;
      this.pending = 0;
      for (const e of this.stack) if (!e.mark) this.push(e);
    }, PENDING_MS);
  }

  /** Close the top overlay as if Back was pressed (Escape, backdrop). */
  back() {
    const e = this.top();
    if (!e) return false;
    this.release(e);
    e.close();
    return true;
  }

  onPop(ev) {
    const prev = this.cur, st = ev.state;
    this.cur = st;
    const swallow = () => { if (ev.stopImmediatePropagation) ev.stopImmediatePropagation(); };
    if (this.returnTo) {   // the step forward below has landed
      swallow();
      this.pending = Math.max(0, this.pending - 1);
      this.returnTo = null;
      if (!this.pending) for (const e of this.stack) if (!e.mark) this.push(e);
      return;
    }
    if (this.pending && !sameRoute(prev, st)) {
      // our step back (an overlay closing) landed after the page had navigated meanwhile
      // (a link inside the overlay): it took the user back to the old page. Undo it
      // quietly so the router never sees either step.
      swallow();
      this.pending--;
      this.returnTo = prev;
      this.go(1, depthOf(prev));
      return;
    }
    if (!sameRoute(prev, st)) {   // a real navigation: not ours
      this.pending = 0;
      const open = this.stack.splice(0);
      for (const e of open.reverse()) e.close();
      // Back from another page onto an overlay entry left behind there: skip it
      if (depthOf(st) && prev && st && prev.idx != null && st.idx != null && st.idx < prev.idx) {
        this.go(-depthOf(st), 0);
      }
      return;
    }
    if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
    const m = depthOf(st);
    if (this.pending) {
      this.pending--;
      if (!this.pending) for (const e of this.stack) if (!e.mark) this.push(e);
      return;
    }
    // Back (or Forward): close everything above the entry we are on now
    const closing = this.stack.filter((e) => e.mark > m);
    this.stack = this.stack.filter((e) => !(e.mark > m));
    for (const e of closing.reverse()) e.close();
    // overlays opened while a step back was in flight have no entry yet
    for (const e of this.stack) if (!e.mark) this.push(e);
    // Forward onto an entry whose overlay is gone: nothing to show, step back again
    const top = this.maxMark();
    if (m > top) this.go(top - m, top);
  }
}
