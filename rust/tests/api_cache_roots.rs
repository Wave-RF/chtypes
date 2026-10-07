//! Public issue #530: `chtypes::cache_root` and `chtypes::search_dirs` report
//! the resolution the fetch layer runs, from the one table every binding's test
//! reads (`tests/fixtures/cache-roots/cases.json`), and create nothing.
//!
//! The environment is process-wide, so everything that sets it is ONE test.

use std::path::{Path, PathBuf};

use chtypes::{FetchOptions, cache_root, search_dirs};

fn table() -> serde_json::Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("tests/fixtures/cache-roots/cases.json");
    serde_json::from_slice(&std::fs::read(path).expect("read cases.json"))
        .expect("parse cases.json")
}

fn sub(v: &str, tmp: &Path) -> String {
    v.replace("<TMP>", &tmp.to_string_lossy())
        .replace("<CWD>", &std::env::current_dir().unwrap().to_string_lossy())
}

fn strings(v: &serde_json::Value, tmp: &Path) -> Vec<String> {
    v.as_array()
        .expect("an array")
        .iter()
        .map(|s| sub(s.as_str().expect("a string"), tmp))
        .collect()
}

#[test]
fn the_shared_table_and_creates_nothing() {
    let tmp = std::env::temp_dir().join(format!("cache-roots-api-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(&tmp).unwrap();
    let table = table();
    for case in table["cases"].as_array().expect("cases") {
        let name = case["name"].as_str().unwrap();
        for k in ["CHTYPES_CACHE", "XDG_CACHE_HOME"] {
            // SAFETY: this is the only test in this binary, so nothing else reads the environment.
            unsafe { std::env::remove_var(k) };
        }
        for (k, v) in case["env"].as_object().unwrap() {
            // SAFETY: as above.
            unsafe { std::env::set_var(k, sub(v.as_str().unwrap(), &tmp)) };
        }
        let options = FetchOptions {
            cache_dir: case["cache_dir"].as_str().map(|s| sub(s, &tmp)),
            system_dirs: match &case["system_dirs"] {
                serde_json::Value::Null => None,
                v => Some(strings(v, &tmp).into_iter().map(PathBuf::from).collect()),
            },
            ..FetchOptions::default()
        };
        let want: Vec<PathBuf> = strings(&case["search_dirs"], &tmp)
            .into_iter()
            .map(PathBuf::from)
            .collect();
        assert_eq!(search_dirs(&options).unwrap(), want, "{name}");
        assert_eq!(cache_root(&options).unwrap(), want[0], "{name}");
    }
    // Nothing the cases named exists: a lookup creates nothing.
    let left: Vec<_> = std::fs::read_dir(&tmp).unwrap().collect();
    assert!(left.is_empty(), "the lookups created {left:?}");
    let _ = std::fs::remove_dir_all(&tmp);
}
