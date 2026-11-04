//! [`CancelToken`]: the caller's cancellation source for the cancelable calls
//! (`chs_cancel` in `spec/abi-v2/docs.md`).
//!
//! The C token is per call and per image and never public. With a token in a
//! call's options, the call creates a C token, registers it with the
//! [`CancelToken`] (which fires it at once if the token was already canceled),
//! names its id in the call's options document, makes the call, unregisters it
//! (waiting for any fire already in progress) and only then frees it. Without
//! a token nothing changes: no C token is created and no extra ABI call is
//! made.
//!
//! Fire-before-free is the rule this module exists to keep: a fire runs under
//! the token's lock, and so does the unregistration that precedes every free,
//! so a fire can never reach a freed C token.

use std::sync::{Arc, Mutex, MutexGuard};

use crate::abi2::calls_gen::CancelHandle;
use crate::abi2::decls::{Api, ChsCancel};
use crate::error::{Error, Result};

/// A cancellation source: [`CancelToken::cancel`] stops every call that was
/// given this token and has not yet passed its last check point.
///
/// `Clone + Send + Sync`: a clone is the same source, so one token, cloned
/// into several calls' options and threads, cancels all of them together.
/// Canceling is one-shot and idempotent; a token never resets, so make a new
/// one per request. A call given a token that is already canceled is still
/// made, and the library answers it canceled at entry, reading no byte of the
/// body.
///
/// A canceled call returns [`Error::Canceled`] (`CHS_CANCELED`, reason
/// `call-canceled`) and no output. A call that passed its last check point
/// before the cancel returns its complete answer: a complete answer is never
/// turned into a cancel. Where a call can be interrupted is the library's own
/// check points (`spec/abi-v2/docs.md`, `chs_cancel`); one long action runs to
/// its end first.
///
/// Two tokens are equal only when one is a clone of the other.
#[derive(Clone, Default)]
pub struct CancelToken {
    inner: Arc<Mutex<State>>,
}

#[derive(Default)]
struct State {
    canceled: bool,
    next_key: u64,
    inflight: Vec<(u64, Registered)>,
}

/// One C token registered with a [`CancelToken`]: what a fire needs.
struct Registered {
    api: Arc<Api>,
    ptr: *const ChsCancel,
}

// SAFETY: `chs_cancel_fire` is callable from any thread, and the pointer is
// only fired while it is registered, which is always before its free (see
// `Registration`'s drop and `call_with`).
unsafe impl Send for Registered {}

impl Registered {
    fn fire(&self) {
        // SAFETY: the token is registered, so its handle has not been freed:
        // `call_with` frees it only after the registration is removed under
        // this same lock. `chs_cancel_fire` is safe from any thread and never
        // blocks.
        unsafe { (self.api.chs_cancel_fire)(self.ptr) }
    }
}

impl CancelToken {
    /// A new token, not canceled.
    pub fn new() -> CancelToken {
        CancelToken::default()
    }

    /// Cancel every call given this token, now and later. From any thread;
    /// idempotent; never blocks on a call.
    pub fn cancel(&self) {
        let mut state = self.lock();
        if state.canceled {
            return;
        }
        state.canceled = true;
        for (_, r) in &state.inflight {
            r.fire();
        }
    }

    /// Whether [`CancelToken::cancel`] has been called on this token or a
    /// clone of it.
    pub fn is_canceled(&self) -> bool {
        self.lock().canceled
    }

    fn lock(&self) -> MutexGuard<'_, State> {
        // A poisoned lock only means a thread panicked while holding it; the
        // state (a flag and a list) is still valid.
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Register a C token; a token registered after `cancel` is fired at once.
    fn register(&self, api: &Arc<Api>, handle: &CancelHandle) -> Registration<'_> {
        let mut state = self.lock();
        let r = Registered {
            api: Arc::clone(api),
            ptr: handle.as_ptr(),
        };
        if state.canceled {
            r.fire();
        }
        let key = state.next_key;
        state.next_key += 1;
        state.inflight.push((key, r));
        Registration { token: self, key }
    }
}

impl PartialEq for CancelToken {
    fn eq(&self, other: &CancelToken) -> bool {
        Arc::ptr_eq(&self.inner, &other.inner)
    }
}

impl Eq for CancelToken {}

impl std::fmt::Debug for CancelToken {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CancelToken")
            .field("canceled", &self.is_canceled())
            .finish_non_exhaustive()
    }
}

/// A registration; dropping it removes the C token under the lock, so once it
/// is dropped no fire is in progress on it and none can start.
struct Registration<'a> {
    token: &'a CancelToken,
    key: u64,
}

impl Drop for Registration<'_> {
    fn drop(&mut self) {
        let mut state = self.token.lock();
        state.inflight.retain(|(k, _)| *k != self.key);
    }
}

/// Make one cancelable call. `f` receives the C token's id to name in the
/// call's options document, or `None` when the caller gave no token, in which
/// case nothing else happens.
pub(crate) fn call_with<T>(
    api: &Arc<Api>,
    token: Option<&CancelToken>,
    f: impl FnOnce(Option<&str>) -> Result<T>,
) -> Result<T> {
    let Some(token) = token else {
        return f(None);
    };
    let (handle, id) = api.cancel_create().map_err(Error::from_call)?;
    let id = String::from_utf8(id)
        .map_err(|_| Error::internal("chs_cancel_create returned an id that is not ASCII"))?;
    let registration = token.register(api, &handle);
    let out = f(Some(&id));
    // Unregister first (waiting for a fire in progress), then free: the order
    // is the fire-before-free rule. On a panic in `f` the locals drop in this
    // same order, the registration before the handle.
    drop(registration);
    drop(handle);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_token_is_clone_send_sync_and_equal_only_to_its_clones() {
        fn shareable<T: Clone + Send + Sync + 'static>() {}
        shareable::<CancelToken>();
        let a = CancelToken::new();
        assert_eq!(a, a.clone());
        assert_ne!(a, CancelToken::new());
    }

    #[test]
    fn cancel_is_one_shot_and_idempotent_and_seen_by_every_clone() {
        let a = CancelToken::new();
        let b = a.clone();
        assert!(!a.is_canceled());
        b.cancel();
        b.cancel();
        assert!(a.is_canceled());
        let from_thread = a.clone();
        std::thread::spawn(move || from_thread.cancel())
            .join()
            .unwrap();
        assert!(a.is_canceled());
    }
}
