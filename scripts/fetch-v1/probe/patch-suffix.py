#!/usr/bin/env python3
"""PROBE ONLY (do not merge): add two env-gated modes to a checkout's Rust
`unique_suffix()` (rust/src/ocifetch/layout.rs), for the amplified and the
forced-collision legs. Without either variable the binary behaves exactly as
the commit does.

  PROBE_CLOCK_STEP_NS=<n>    the commit's OLD naming (clock + thread id), on a
                             clock rounded down to n ns: a coarse clock, so the
                             natural collision happens often enough to count.
  PROBE_COLLIDE_SUFFIX=<s>   every other draw in a process returns <s>, so the
                             first name each process draws repeats across
                             processes: a planted collision, the same for any
                             commit's naming.

    patch-suffix.py <path to layout.rs>
"""

import sys
from pathlib import Path

HOOK = """fn unique_suffix() -> String {
    // PROBE ONLY (do not merge).
    if let Ok(fixed) = std::env::var("PROBE_COLLIDE_SUFFIX") {
        static PROBE_DRAWS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        if PROBE_DRAWS.fetch_add(1, std::sync::atomic::Ordering::Relaxed) % 2 == 0 {
            return fixed;
        }
    }
    if let Some(step) = std::env::var("PROBE_CLOCK_STEP_NS").ok().and_then(|s| s.parse::<u128>().ok()) {
        let step = step.max(1);
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        return format!("{:x}-{:?}", nanos / step * step, std::thread::current().id());
    }
"""

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
anchor = "fn unique_suffix() -> String {\n"
if text.count(anchor) != 1:
    sys.exit(f"patch-suffix: expected exactly one {anchor.strip()!r} in {path}, found {text.count(anchor)}")
path.write_text(text.replace(anchor, HOOK, 1), encoding="utf-8")
print(f"patch-suffix: patched {path}")
