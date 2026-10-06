//! The process setup (`docs/reference/bindings-v1.md` §6): the image zone and
//! the default settings, chosen once, before traffic.
//!
//! Both are process-wide in ClickHouse, so [`setup`] records them and every
//! image is set up at loader step 7, once. The rules, the same in all four
//! bindings:
//!
//! 1. [`setup`] records; it does not load. Called again with the same zone
//!    spelling, byte for byte, and the same defaults, it is a no-op. A
//!    different zone or different defaults is [`Error::Usage`] naming both; the
//!    first setup stands. This is the library's own process-once rule for
//!    `chs_initialize`, applied here while there is no image yet to ask.
//! 2. It latches on the first successful load step 7. If [`setup`] was never
//!    called, the first open records the empty setup: the empty zone, which
//!    the library reads as `UTC`, and no defaults. Once an image completes
//!    step 7 (`chs_initialize`, then `chs_set_defaults` when there are
//!    defaults), [`setup`] succeeds only with exactly the setup in effect. If
//!    step 7 fails before any image has completed it, the record is cleared,
//!    so a corrected [`setup`] is accepted and the next open runs step 7 with
//!    it. Call [`setup`] first.
//! 3. There is no public setter for defaults during traffic.

use std::sync::Mutex;

use serde_json::{Map, Value as Json};

use crate::error::{Error, Result};

/// The image zone and the default settings, chosen once per process.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SetupOptions {
    /// The image zone, compared byte for byte and never canonicalized. `None`
    /// (or empty) is the library's default, `UTC`. A name ClickHouse's own
    /// `DateLUT` cannot load is refused by the library at the first open.
    pub timezone: Option<String>,
    /// The default settings every later call starts from, applied to each
    /// image at load. Values are strings, and only strings.
    pub defaults: Vec<(String, String)>,
}

/// What is recorded: the zone as bytes, and the defaults as the JSON object
/// that crosses the C boundary (`None` when there are none).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Recorded {
    pub(crate) timezone: Vec<u8>,
    pub(crate) defaults: Option<Vec<u8>>,
}

/// The setup guard: the record, and whether any image has completed load step
/// 7 under it. Every open holds the image list's lock from its commit to its
/// settle (`crate::library::open_image`), so the record a load ran under is the
/// one it settles.
static STATE: Mutex<State> = Mutex::new(State {
    recorded: None,
    latched: false,
});

struct State {
    recorded: Option<Recorded>,
    latched: bool,
}

fn recorded_of(options: &SetupOptions) -> Result<Recorded> {
    let timezone = options.timezone.clone().unwrap_or_default().into_bytes();
    let defaults = if options.defaults.is_empty() {
        None
    } else {
        let mut map = Map::new();
        for (k, v) in &options.defaults {
            if map.insert(k.clone(), Json::String(v.clone())).is_some() {
                return Err(Error::usage(format!(
                    "setup: the default setting {k:?} is given twice"
                )));
            }
        }
        // serde_json's map is ordered by key, so two orders of one set of
        // defaults serialize identically.
        Some(
            serde_json::to_vec(&Json::Object(map))
                .map_err(|e| Error::internal(format!("setup: defaults do not encode: {e}")))?,
        )
    };
    Ok(Recorded { timezone, defaults })
}

fn describe(r: &Recorded) -> String {
    let zone = if r.timezone.is_empty() {
        "the empty zone (UTC)".to_string()
    } else {
        format!("timezone {:?}", String::from_utf8_lossy(&r.timezone))
    };
    match &r.defaults {
        None => format!("{zone} and no defaults"),
        Some(d) => format!("{zone} and defaults {}", String::from_utf8_lossy(d)),
    }
}

/// Record the image zone and the default settings, once per process. See the
/// module documentation for the rules.
pub fn setup(options: SetupOptions) -> Result<()> {
    let wanted = recorded_of(&options)?;
    let mut state = STATE.lock().unwrap_or_else(|e| e.into_inner());
    match &state.recorded {
        None => {
            state.recorded = Some(wanted);
            Ok(())
        }
        Some(current) if *current == wanted => Ok(()),
        Some(current) => Err(Error::usage(format!(
            "setup is process-wide and already in effect with {}; refusing {}",
            describe(current),
            describe(&wanted)
        ))),
    }
}

/// An open commits the setup in effect before step 7, recording the empty one
/// when [`setup`] was never called.
pub(crate) fn commit() -> Recorded {
    let mut state = STATE.lock().unwrap_or_else(|e| e.into_inner());
    state
        .recorded
        .get_or_insert_with(|| Recorded {
            timezone: Vec::new(),
            defaults: None,
        })
        .clone()
}

/// How load step 7 ended, under the setup guard. A success latches the setup
/// in effect. A failure before any image has completed step 7 clears the
/// record, so [`setup`] accepts a corrected setup; once the setup has latched,
/// a failure changes nothing (the library's own process-once rule answers a
/// different zone on an image that already has one).
pub(crate) fn settle(completed: bool) {
    let mut state = STATE.lock().unwrap_or_else(|e| e.into_inner());
    if completed {
        state.latched = true;
    } else if !state.latched {
        state.recorded = None;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn opts(zone: Option<&str>, defaults: &[(&str, &str)]) -> SetupOptions {
        SetupOptions {
            timezone: zone.map(str::to_string),
            defaults: defaults
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect(),
        }
    }

    // `setup` is process-wide, so the rule is tested on `recorded_of` and a
    // local copy of the state machine's decision, never on the real static (a
    // sibling test could otherwise commit it first). The static itself is
    // driven end to end by tests/api_v1.rs, in its own process.
    fn decide(
        state: &mut Option<Recorded>,
        options: &SetupOptions,
    ) -> std::result::Result<(), String> {
        let wanted = recorded_of(options).map_err(|e| e.to_string())?;
        match state {
            None => {
                *state = Some(wanted);
                Ok(())
            }
            Some(c) if *c == wanted => Ok(()),
            Some(c) => Err(format!("{} vs {}", describe(c), describe(&wanted))),
        }
    }

    #[test]
    fn the_same_setup_twice_is_a_no_op_and_a_different_one_names_both() {
        let mut state = None;
        decide(&mut state, &opts(Some("Europe/Paris"), &[])).unwrap();
        decide(&mut state, &opts(Some("Europe/Paris"), &[])).unwrap();
        let err = decide(&mut state, &opts(Some("Asia/Tokyo"), &[])).unwrap_err();
        assert!(
            err.contains("Europe/Paris") && err.contains("Asia/Tokyo"),
            "{err}"
        );
        // The first stands.
        decide(&mut state, &opts(Some("Europe/Paris"), &[])).unwrap();
    }

    #[test]
    fn the_zone_is_compared_byte_for_byte_never_canonicalized() {
        let mut state = None;
        decide(&mut state, &opts(Some("UTC"), &[])).unwrap();
        // The empty spelling and "UTC" are the same zone to the library and
        // different spellings here, exactly as the library's process-once rule.
        assert!(decide(&mut state, &opts(None, &[])).is_err());
    }

    #[test]
    fn defaults_compare_as_a_set_and_a_duplicate_key_is_misuse() {
        let a = recorded_of(&opts(None, &[("a", "1"), ("b", "2")])).unwrap();
        let b = recorded_of(&opts(None, &[("b", "2"), ("a", "1")])).unwrap();
        assert_eq!(a, b);
        assert_eq!(a.defaults.as_deref(), Some(&br#"{"a":"1","b":"2"}"#[..]));
        assert!(matches!(
            recorded_of(&opts(None, &[("a", "1"), ("a", "2")])),
            Err(Error::Usage(_))
        ));
        assert_eq!(recorded_of(&opts(None, &[])).unwrap().defaults, None);
    }
}
