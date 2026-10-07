//! `chtypes`: the command line over the v1 fetch layer
//! (`docs/guides/fetch-v1.md`), spelled identically in every SDK.
//!
//! 2.0.0-dev: UNSTABLE, staging only, not for production. This CLI speaks the
//! ABI v2 dev channel (`spec/abi-v2/docs.md`, rules r5 and r6): it fetches only
//! from `https://registry-staging.wavehouse.dev/chtypes/v2-dev` and trusts only
//! the staging key; `CHTYPES_ARTIFACTS_URL`, `CHTYPES_TRUSTED_KEYS` and
//! `CHTYPES_ALLOW_UNSIGNED` are ignored, each with one warning; `--lock`,
//! `--frozen` and `--update` are refused before any network call; its default
//! cache is `${XDG_CACHE_HOME:-~/.cache}/chtypes/v2-dev`, and `--cache DIR` (or
//! `CHTYPES_CACHE`) is used through its subroot `DIR/v2-dev`.
//!
//! ```text
//! chtypes fetch <spelling>... | --all  [--platform <key>] [--cache <dir>] [--lock <file>] [--frozen] [--offline] [--update] [--strict]
//! chtypes verify [--cache <dir>] [--strict]       re-verify the installed cache
//! chtypes list [--cache <dir>] [--offline] [--strict]   installed builds, and the published lines unless --offline
//! chtypes where [--cache <dir>] [--strict]        the cache root (the v2-dev one)
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

  chtypes fetch <spelling>... [--platform <os-arch>] [--cache <dir>] [--lock <file>] [--frozen] [--offline] [--update] [--strict]
  chtypes fetch --all         [--platform <os-arch>] [--cache <dir>] [--lock <file>] [--frozen] [--offline] [--update] [--strict]
  chtypes verify [--cache <dir>] [--strict]            re-verify the installed cache
  chtypes list [--cache <dir>] [--offline] [--strict]  installed builds; without --offline, published lines too
  chtypes where [--cache <dir>] [--strict]             the cache root (<dir>/v2-dev)
  chtypes --version                         chtypes <version>
  chtypes -h | --help                       this text

A <spelling> is a two-, three- or four-part version (26.8, 26.8.15, 26.8.15.10):
no 'v' prefix and no channel suffix. --all fetches every line (two-part tag)
the registry publishes. --lock writes the lock after a fetch; --frozen fetches
exactly what the lock pins (default file chtypes.lock) and does no discovery;
--offline reads the cache only; --update re-resolves every locked request and
rewrites the lock (it requires --lock). `fetch` prints each installed directory.
--strict (or CHTYPES_CACHE_STRICT=1): a cache that cannot be read is
CHTYPES_CACHE_UNUSABLE, never not-installed.

Environment: CHTYPES_CACHE (this 2.0.0-dev SDK uses its subroot
<CHTYPES_CACHE>/v2-dev), CHTYPES_DOWNLOAD_TOKEN, CHTYPES_CACHE_STRICT.

2.0.0-dev: UNSTABLE, staging only, not for production. Fetches only from the
staging dev channel and trusts only its key; --lock, --frozen and --update are
refused; CHTYPES_ARTIFACTS_URL, CHTYPES_TRUSTED_KEYS and CHTYPES_ALLOW_UNSIGNED
are ignored (spec/abi-v2/docs.md, rule r6).
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
    cache: Option<String>,
    frozen: bool,
    offline: bool,
    update: bool,
    strict: bool,
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
            "--strict" => args.strict = true,
            "--platform" | "--lock" | "--cache" => {
                let value = it
                    .next()
                    .filter(|v| !v.starts_with("--"))
                    .cloned()
                    .ok_or_else(|| Usage(format!("{arg} needs a value")))?;
                match arg.as_str() {
                    "--platform" => args.platform = Some(value),
                    "--cache" => args.cache = Some(value),
                    _ => args.lock = Some(PathBuf::from(value)),
                }
            }
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
        args.frozen
            .then(|| PathBuf::from(constants::LOCK_DEFAULT_FILE))
    });
    Options {
        platform: args.platform.clone(),
        cache_dir: args.cache.clone(),
        offline: args.offline,
        frozen: args.frozen,
        update: args.update,
        lock_write: args.lock.is_some() && !args.frozen,
        lock_path,
        strict_cache: args.strict.then_some(true),
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
    // -h and --help print the usage to stdout and exit 0 wherever they appear.
    if argv
        .iter()
        .take_while(|a| a.as_str() != "--")
        .any(|a| a == "-h" || a == "--help")
    {
        print!("{USAGE}");
        return ExitCode::SUCCESS;
    }
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

/// `--platform` is a `fetch` flag only.
fn no_platform(command: &str, args: &Args) -> Result<(), Usage> {
    match args.platform {
        Some(_) => Err(Usage(format!(
            "--platform applies to fetch only, not {command}"
        ))),
        None => Ok(()),
    }
}

fn cmd_fetch(args: &Args) -> Result<u8, Usage> {
    // A dev SDK pins nothing: the refusal comes before anything else, every
    // other check of the arguments and the network above all
    // (spec/abi-v2/docs.md, rule r6).
    if let Some(refusal) = ocifetch::channel::refuse_pinning(&options(args)) {
        return Err(Usage(refusal));
    }
    if args.all && !args.spellings.is_empty() {
        return Err(Usage(format!(
            "--all fetches every published line; drop the spelling ({})",
            args.spellings.join(" ")
        )));
    }
    if !args.all && args.spellings.is_empty() {
        return Err(Usage("a version spelling is required (or --all)".into()));
    }
    if args.update && args.lock.is_none() {
        return Err(Usage("--update requires --lock".into()));
    }
    if args.offline && args.update {
        return Err(Usage(
            "--update must reach the registry; it cannot combine with --offline".into(),
        ));
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
    no_platform("verify", args)?;
    if !args.spellings.is_empty() || args.all {
        return Err(Usage("verify takes no arguments".into()));
    }
    let results = match ensure::verify_installed(options(args)) {
        Ok(r) => r,
        Err(e) => return Ok(report(&e)),
    };
    if results.is_empty() {
        // An empty pass must never look like a good one (public issue #486):
        // say that nothing was verified, and why when the cache says why. In
        // strict mode it is a failure, so a mounted cache can be health-checked.
        let root = ocifetch::layout::cache_root(args.cache.as_deref())
            .map(|r| r.display().to_string())
            .unwrap_or_default();
        eprintln!("chtypes: verified 0 builds under {root}");
        print_notes(args);
        if ocifetch::faults::strict_mode(options(args).strict_cache) {
            return Ok(report(&Error::ArtifactMissing(format!(
                "no build is installed under {root}, and strict mode needs one"
            ))));
        }
        return Ok(0);
    }
    print_notes(args);
    let mut bad = 0usize;
    for r in &results {
        if !r.ok {
            bad += 1;
            eprintln!("chtypes: CORRUPT {}: {}", r.dir.display(), r.detail);
        }
    }
    Ok(if bad == 0 {
        0
    } else {
        exit_for(&Error::ArtifactCorrupt(String::new()))
    })
}

fn cmd_list(args: &Args) -> Result<u8, Usage> {
    no_platform("list", args)?;
    if !args.spellings.is_empty() || args.all {
        return Err(Usage("list takes no arguments".into()));
    }
    match ensure::list_installed(options(args)) {
        Ok(list) => {
            for r in list {
                println!("installed {} {} {}", r.version, r.platform, r.dir.display());
            }
        }
        Err(e) => return Ok(report(&e)),
    }
    print_notes(args);
    if args.offline {
        return Ok(0);
    }
    match ocifetch::tags::published_versions(&options(args)) {
        Ok(versions) => {
            for v in versions {
                println!("published {v} support unknown");
            }
            Ok(0)
        }
        Err(e) => Ok(report(&e)),
    }
}

fn cmd_where(args: &Args) -> Result<u8, Usage> {
    no_platform("where", args)?;
    if !args.spellings.is_empty() || args.all {
        return Err(Usage("where takes no arguments".into()));
    }
    if ocifetch::faults::strict_mode(options(args).strict_cache) {
        // Strict mode checks the root before naming it.
        if let Err(e) = ensure::probe_cache(options(args)) {
            return Ok(report(&e));
        }
    }
    match ocifetch::layout::cache_root(args.cache.as_deref()) {
        Ok(root) => {
            println!("{}", root.display());
            Ok(0)
        }
        Err(e) => Ok(report(&e)),
    }
}

/// What the cache says about itself in the default mode: a warning per root
/// or entry it could not read, and the 0.x hint.
fn print_notes(args: &Args) {
    for note in ensure::missing_notes(&options(args)) {
        eprintln!("chtypes: {note}");
    }
}
