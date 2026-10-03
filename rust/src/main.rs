//! `chtypes`: the command line over the v1 fetch layer
//! (`docs/guides/fetch-v1.md`), spelled identically in every SDK.
//!
//! ```text
//! chtypes fetch <spelling>... | --all  [--platform <key>] [--lock <file>] [--frozen] [--offline] [--update]
//! chtypes verify                       re-verify the installed cache
//! chtypes list                         the version spellings the registry publishes
//! chtypes where                        the v1 cache root
//! ```
//!
//! Progress and warnings go to stderr; results go to stdout. Exit statuses
//! come from the fetch layer's error table (`docs/guides/fetch-v1.md` §8, the
//! generated `ERROR_EXIT_CODES`); a usage error exits 2.

// The fetch layer is not public API, so the binary compiles it in under its
// own crate root, the same way the conformance suites do.
#[path = "ocifetch/mod.rs"]
mod ocifetch;

use std::path::PathBuf;
use std::process::ExitCode;

use ocifetch::constants;
use ocifetch::ensure::{self, Options, Resolved};
use ocifetch::error::Error;
use ocifetch::oci::VersionRequest;

/// The one status this program owns: a usage error. Everything else is the
/// fetch layer's table.
const EXIT_USAGE: u8 = 2;

const USAGE: &str = "\
chtypes: fetch, verify and list ClickHouse artifacts for the chtypes SDKs

  chtypes fetch <spelling>... [--platform <key>] [--lock <file>] [--frozen] [--offline] [--update]
  chtypes fetch --all         [--platform <key>] [--lock <file>] [--frozen] [--offline] [--update]
  chtypes verify              re-verify the installed cache
  chtypes list                the version spellings the registry publishes
  chtypes where               the v1 cache root

A <spelling> is a two-, three- or four-part version (26.8, 26.8.15, 26.8.15.10):
no 'v' prefix and no channel suffix. --all fetches every line (two-part tag)
the registry publishes. --lock writes the lock after a fetch; --frozen fetches
exactly what the lock pins (default file chtypes.lock) and does no discovery;
--offline reads the cache only; --update re-resolves and rewrites the lock.
`fetch` prints each installed directory on stdout.

Environment: CHTYPES_ARTIFACTS_URL, CHTYPES_CACHE, CHTYPES_DOWNLOAD_TOKEN,
CHTYPES_TRUSTED_KEYS, CHTYPES_ALLOW_UNSIGNED.
";

/// A usage problem, reported on stderr with exit status 2.
struct Usage(String);

#[derive(Default)]
struct Args {
    command: String,
    spellings: Vec<String>,
    all: bool,
    platform: Option<String>,
    lock: Option<PathBuf>,
    frozen: bool,
    offline: bool,
    update: bool,
}

fn parse(argv: &[String]) -> Result<Args, Usage> {
    let mut args = Args::default();
    let mut it = argv.iter();
    let Some(command) = it.next() else {
        return Err(Usage(String::new()));
    };
    args.command = command.clone();
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--all" => args.all = true,
            "--frozen" => args.frozen = true,
            "--offline" => args.offline = true,
            "--update" => args.update = true,
            "--platform" | "--lock" => {
                let value = it
                    .next()
                    .filter(|v| !v.starts_with("--"))
                    .cloned()
                    .ok_or_else(|| Usage(format!("{arg} needs a value")))?;
                if arg == "--platform" {
                    args.platform = Some(value);
                } else {
                    args.lock = Some(PathBuf::from(value));
                }
            }
            "-h" | "--help" => return Err(Usage(String::new())),
            other if other.starts_with('-') => {
                return Err(Usage(format!("unknown flag: {other}")));
            }
            other => args.spellings.push(other.to_string()),
        }
    }
    Ok(args)
}

fn options(args: &Args) -> Options {
    let lock_path = args.lock.clone().or_else(|| {
        (args.frozen || args.update).then(|| PathBuf::from(constants::LOCK_DEFAULT_FILE))
    });
    Options {
        platform: args.platform.clone(),
        offline: args.offline,
        frozen: args.frozen,
        update: args.update,
        lock_write: args.lock.is_some() && !args.frozen,
        lock_path,
        ..Options::default()
    }
}

/// An error's own exit status, from the generated table.
fn exit_for(err: &Error) -> u8 {
    // The table has an entry for every code this module can raise; a drift
    // between the two reads as a plain failure, never as success.
    err.exit_code().unwrap_or(1)
}

fn report(err: &Error) -> u8 {
    eprintln!("chtypes: {err}");
    exit_for(err)
}

fn print_resolved(resolved: &Resolved) {
    for warning in &resolved.warnings {
        eprintln!("chtypes: warning: {warning}");
    }
    eprintln!(
        "chtypes: {} {} ({}){}",
        resolved.version,
        resolved.platform,
        resolved.source,
        if resolved.already_installed {
            ", already installed"
        } else {
            ""
        }
    );
    println!("{}", resolved.dir.display());
}

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    if argv.first().map(String::as_str) == Some("--version") {
        println!("chtypes {}", env!("CARGO_PKG_VERSION"));
        return ExitCode::SUCCESS;
    }
    let args = match parse(&argv) {
        Ok(a) => a,
        Err(Usage(message)) => return usage(&message),
    };
    let result = match args.command.as_str() {
        "fetch" => cmd_fetch(&args),
        "verify" => cmd_verify(&args),
        "list" => cmd_list(&args),
        "where" => cmd_where(&args),
        "help" => {
            eprint!("{USAGE}");
            return ExitCode::SUCCESS;
        }
        other => Err(Usage(format!("unknown command: {other}"))),
    };
    match result {
        Ok(code) => ExitCode::from(code),
        Err(Usage(message)) => usage(&message),
    }
}

fn usage(message: &str) -> ExitCode {
    if !message.is_empty() {
        eprintln!("chtypes: {message}");
    }
    eprint!("{USAGE}");
    ExitCode::from(EXIT_USAGE)
}

fn cmd_fetch(args: &Args) -> Result<u8, Usage> {
    if args.all && !args.spellings.is_empty() {
        return Err(Usage(format!(
            "--all fetches every published line; drop the spelling ({})",
            args.spellings.join(" ")
        )));
    }
    if !args.all && args.spellings.is_empty() {
        return Err(Usage("a version spelling is required (or --all)".into()));
    }
    if args.frozen && args.update {
        return Err(Usage(
            "--frozen fetches what the lock pins; --update rewrites it: pass one".into(),
        ));
    }
    // Every spelling is checked before the first fetch: a typo in the third
    // argument must not cost the first two downloads.
    for spelling in &args.spellings {
        match VersionRequest::parse(spelling) {
            Ok(r) if !r.is_literal() => {}
            Ok(_) => {
                return Err(Usage(format!(
                    "{spelling:?} is not a version spelling (two, three or four numeric parts)"
                )));
            }
            Err(e) => return Err(Usage(e.to_string())),
        }
    }
    let spellings = if args.all {
        match ocifetch::tags::published_versions(&options(args)) {
            Ok(all) => all
                .into_iter()
                .filter(|v| v.matches('.').count() == 1)
                .collect(),
            Err(e) => return Ok(report(&e)),
        }
    } else {
        args.spellings.clone()
    };
    for spelling in &spellings {
        match ensure::ensure(spelling, options(args)) {
            Ok(resolved) => print_resolved(&resolved),
            Err(e) => return Ok(report(&e)),
        }
    }
    Ok(0)
}

fn cmd_verify(args: &Args) -> Result<u8, Usage> {
    if !args.spellings.is_empty() || args.all {
        return Err(Usage("verify takes no arguments".into()));
    }
    let results = match ensure::verify_installed(options(args)) {
        Ok(r) => r,
        Err(e) => return Ok(report(&e)),
    };
    if results.is_empty() {
        eprintln!("chtypes: nothing is installed");
        return Ok(0);
    }
    let mut bad = 0usize;
    for r in &results {
        if r.ok {
            println!("ok       {}", r.dir.display());
        } else {
            bad += 1;
            println!("CORRUPT  {}  {}", r.dir.display(), r.detail);
        }
    }
    eprintln!(
        "chtypes: {} installed build(s) re-verified, {bad} corrupt",
        results.len()
    );
    Ok(if bad == 0 {
        0
    } else {
        exit_for(&Error::ArtifactCorrupt(String::new()))
    })
}

fn cmd_list(args: &Args) -> Result<u8, Usage> {
    if !args.spellings.is_empty() || args.all {
        return Err(Usage("list takes no arguments".into()));
    }
    match ocifetch::tags::published_versions(&options(args)) {
        Ok(versions) => {
            for v in versions {
                println!("{v}");
            }
            Ok(0)
        }
        Err(e) => Ok(report(&e)),
    }
}

fn cmd_where(args: &Args) -> Result<u8, Usage> {
    if !args.spellings.is_empty() || args.all {
        return Err(Usage("where takes no arguments".into()));
    }
    match ocifetch::layout::cache_root(None) {
        Ok(root) => {
            println!("{}", root.display());
            Ok(0)
        }
        Err(e) => Ok(report(&e)),
    }
}
