import { describe, expect, it } from 'vitest'
import { BackStack, MARK } from './backStack'
import { bindBackClose } from './useBackClose'

// A browser window: pushState/replaceState are sync, go() fires popstate later.
function fakeWindow(initial = null) {
  const listeners = []
  const win = {
    entries: [initial], i: 0, popped: [],
    history: {
      get state() { return win.entries[win.i] },
      pushState(s) { win.entries.splice(win.i + 1); win.entries.push(structuredClone(s)); win.i++ },
      replaceState(s) { win.entries[win.i] = structuredClone(s) },
      go(n) {
        setTimeout(() => {
          const j = win.i + n
          if (n === 0 || j < 0 || j >= win.entries.length) return
          win.i = j
          const ev = { state: win.entries[j], stopped: false, stopImmediatePropagation() { this.stopped = true } }
          for (const fn of listeners) { fn(ev); if (ev.stopped) break }
          win.popped.push(ev)
        }, 1)
      },
      back() { this.go(-1) },
    },
    addEventListener(type, fn) { if (type === 'popstate') listeners.push(fn) },
  }
  return win
}
const settle = () => new Promise(resolve => setTimeout(resolve, 10))
const pressBack = async win => { win.history.back(); await settle() }

describe('BackStack + bindBackClose', () => {
  it('Back closes the card dialog instead of leaving the page; the router never sees it', async () => {
    const win = fakeWindow({ usr: null, key: 'list', idx: 2 })
    const stack = new BackStack(win)
    const routerSaw = []
    win.addEventListener('popstate', ev => routerSaw.push(ev.state))
    let open = true
    bindBackClose(stack, () => () => { open = false })
    expect(win.history.state).toEqual({ usr: null, key: 'list', idx: 2, [MARK]: 1 })
    await pressBack(win)
    expect(open).toBe(false)
    expect(win.history.state).toEqual({ usr: null, key: 'list', idx: 2 })
    expect(routerSaw).toEqual([])
  })

  it('closing with ✕ / backdrop removes the history entry again', async () => {
    const win = fakeWindow({ key: 'k', idx: 0 })
    const stack = new BackStack(win)
    const release = bindBackClose(stack, () => () => {})
    expect(win.i).toBe(1)
    release()
    await settle()
    expect(win.i).toBe(0)
  })

  it('nested: the zoom closes first, then the card', async () => {
    const win = fakeWindow({ key: 'k', idx: 0 })
    const stack = new BackStack(win)
    const closed = []
    bindBackClose(stack, () => () => closed.push('card'))
    bindBackClose(stack, () => () => closed.push('zoom'))
    await pressBack(win)
    expect(closed).toEqual(['zoom'])
    await pressBack(win)
    expect(closed).toEqual(['zoom', 'card'])
  })

  it('mayStay: a handler that asks first keeps the overlay reachable by the next Back', async () => {
    const win = fakeWindow({ key: 'k', idx: 0 })
    const stack = new BackStack(win)
    let asked = 0
    bindBackClose(stack, () => () => { asked++ }, { mayStay: true })   // e.g. "discard staged photos?" → Cancel
    await pressBack(win)
    expect(asked).toBe(1)
    expect(stack.size).toBe(1)
    expect(win.history.state[MARK]).toBe(1)
    await pressBack(win)
    expect(asked).toBe(2)
  })

  it('no close handler right now (busy): the overlay stays', async () => {
    const win = fakeWindow({ key: 'k', idx: 0 })
    const stack = new BackStack(win)
    bindBackClose(stack, () => undefined)
    await pressBack(win)
    expect(stack.size).toBe(1)
  })

  it('a link inside an overlay replaces its entry: Back from the new page goes to the old page', async () => {
    const win = fakeWindow({ key: 'set', idx: 4 })
    const stack = new BackStack(win)
    const release = bindBackClose(stack, () => () => {})
    win.history.pushState({ key: 'card', idx: 5 }, '')   // React Router navigates
    release()                                             // the dialog unmounts with the page
    await settle()
    expect(win.entries).toEqual([{ key: 'set', idx: 4 }, { key: 'card', idx: 5 }])
    expect(win.i).toBe(1)
  })
})
