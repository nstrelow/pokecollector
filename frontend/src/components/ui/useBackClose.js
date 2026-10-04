import { useEffect, useRef } from 'react'
import { BackStack } from './backStack'

// One BackStack per page. installBackStack() runs in main.jsx before the router mounts,
// so this popstate listener comes first and can keep overlay-only pops from the router.
let shared = null
export function installBackStack(win = globalThis) {
  if (!shared && win?.history && typeof win.addEventListener === 'function') shared = new BackStack(win)
  return shared
}

/**
 * Register an open overlay with the BackStack. Returns a release function for when it
 * closes by itself. Exported for tests; components use useBackClose.
 *   stack:    a BackStack
 *   onClose:  () => the current close handler
 *   mayStay:  the handler may keep the overlay open (it asks to confirm first, or steps
 *             back inside the overlay, e.g. un-zooms). The overlay then takes a new
 *             history entry before the handler runs, so it stays under anything the
 *             handler opens (a confirm dialog) and the next Back reaches it again; if it
 *             does close, its cleanup steps back over that entry.
 */
export function bindBackClose(stack, onClose, { mayStay = false } = {}) {
  let handle = null
  const register = () => {
    handle = stack.open(() => {
      const close = onClose()
      if (mayStay || !close) register()   // no handler right now (e.g. busy): it stays
      close?.()
    })
  }
  register()
  return () => stack.release(handle)
}

/**
 * The phone's Back button closes this overlay (instead of leaving the page) while isOpen.
 * Closing it any other way (✕, backdrop, Escape, a finished action) removes its history
 * entry again. Nested overlays close one per Back, the top one first. Pass mayStay when
 * onClose can decide not to close (see bindBackClose).
 */
export function useBackClose(isOpen, onClose, { mayStay = false } = {}) {
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    const stack = installBackStack()
    if (!isOpen || !stack) return undefined
    return bindBackClose(stack, () => onCloseRef.current, { mayStay })
  }, [isOpen, mayStay])
}
