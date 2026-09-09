//! `chtypes` — the fetch command every SDK spells identically (`docs/fetch.md`
//! §6), over [`chtypes::ensure`].
//!
//! ```text
//! chtypes fetch <line>... [--all] [--platform <os-arch>] [--dest <dir>]
//!                         [--tag <t> | --url <base>] [--lock <file>] [--frozen]
//!                         [--force] [--offline]
//! chtypes verify [--dest <dir>]        re-hash every installed line against its manifest
//! chtypes list   [--dest <dir>]        what is installed, and what the release offers
//! chtypes where                        the registry directory fetch would write to
//! ```
//!
//! Progress goes to stderr; `fetch` prints the installed directory alone on
//! stdout. Exit codes: 0 ok · 1 verification failed · 2 usage · 3 source
//! unreachable · 4 not published for this platform/line.

use std::path::PathBuf;
use std::process::ExitCode;

use chtypes::fetch::{self, EnsureOptions};
use chtypes::{
    CODE_ARTIFACT_UNPUBLISHED, CODE_SOURCE_UNREACHABLE, Error, host_platform, installed_lines,
};

const USAGE: &str = "\
chtypes — fetch, verify and list ClickHouse artifacts for the chtypes SDKs

  chtypes fetch <line>... [--all] [--platform <os-arch>] [--dest <dir>]
                          [--tag <t> | --url <base>] [--lock <file>] [--frozen]
                          [--force] [--offline]
  chtypes verify [--dest <dir>]        re-hash every installed line against its manifest
  chtypes list   [--dest <dir>]        what is installed, and what the release offers
  chtypes where                        the registry directory fetch would write to

A <line> is a ClickHouse minor line (25.8) or an exact patch (25.8.28.1-lts, a
hard requirement). --all installs every line the release publishes for the
platform. Progress prints on stderr; `fetch` prints each installed directory
alone on stdout.

Environment: CHTYPES_REGISTRY (where to install and look), CHTYPES_ARTIFACTS_URL
(the artifacts host), CHTYPES_TRUSTED_KEYS (hex keys replacing the release key),
CHTYPES_ALLOW_UNSIGNED=1 (skip the signature, loudly), CHTYPES_AUTOFETCH=1.

Exit codes: 0 ok · 1 verification failed · 2 usage · 3 source unreachable ·
4 not published for this platform/line.
";

/// Exit 2: a usage problem, reported on stderr.
struct Usage(String);

/// The flags every subcommand accepts (each ignores what it does not use).
#[derive(Default)]
struct Args {
    command: String,
    lines: Vec<String>,
    all: bool,
    platform: Option<String>,
    dest: Option<PathBuf>,
    tag: Option<String>,
    url: Option<String>,
    lock: Option<PathBuf>,
    frozen: bool,
    force: bool,
    offline: bool,
}

fn parse(argv: &[String]) -> Result<Args, Usage> {
    let mut args = Args::default();
    let mut it = argv.iter();
    let Some(command) = it.next() else {
        return Err(Usage(String::new()));
    };
    args.command = command.clone();
    let value = |flag: &str, it: &mut std::slice::Iter<'_, String>| -> Result<String, Usage> {
        it.next()
            .filter(|v| !v.starts_with("--"))
            .cloned()
            .ok_or_else(|| Usage(format!("{flag} needs a value")))
    };
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--all" => args.all = true,
            "--frozen" => args.frozen = true,
            "--force" => args.force = true,
            "--offline" => args.offline = true,
            "--platform" => args.platform = Some(value(arg, &mut it)?),
            // --out is the spelling the CI workflows use for scripts/fetch.sh.
            "--dest" | "--out" => args.dest = Some(PathBuf::from(value(arg, &mut it)?)),
            "--tag" => args.tag = Some(value(arg, &mut it)?),
            "--url" => args.url = Some(value(arg, &mut it)?),
            "--lock" => args.lock = Some(PathBuf::from(value(arg, &mut it)?)),
            "-h" | "--help" => return Err(Usage(String::new())),
            other if other.starts_with('-') => {
                return Err(Usage(format!("unknown flag: {other}")));
            }
            other => args.lines.push(other.to_string()),
        }
    }
    if args.url.is_some() && args.tag.is_some() {
        return Err(Usage(
            "--url names a full base; --tag selects a release on the artifacts host — pass one"
                .into(),
        ));
    }
    if let Some(p) = &args.platform {
        if !matches!(
            p.as_str(),
            "linux-arm64" | "linux-amd64" | "darwin-arm64" | "darwin-amd64"
        ) {
            return Err(Usage(format!(
                "not a known platform key: {p} ((linux|darwin)-(arm64|amd64))"
            )));
        }
    }
    Ok(args)
}

fn options(args: &Args) -> EnsureOptions {
    EnsureOptions {
        dest: args.dest.clone(),
        platform: args.platform.clone(),
        url: args.url.clone(),
        tag: args.tag.clone(),
        lock: args.lock.clone(),
        frozen: args.frozen,
        force: args.force,
        offline: args.offline,
        trusted_keys: None,
        allow_unsigned: None,
        progress: true,
    }
}

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    if argv.first().map(String::as_str) == Some("--version") {
        println!("chtypes {}", env!("CARGO_PKG_VERSION"));
        return ExitCode::SUCCESS;
    }
    let args = match parse(&argv) {
        Ok(a) => a,
        Err(Usage(message)) => {
            if !message.is_empty() {
                eprintln!("chtypes: {message}");
            }
            eprint!("{USAGE}");
            return ExitCode::from(2);
        }
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
        other => {
            eprintln!("chtypes: unknown command: {other}");
            eprint!("{USAGE}");
            return ExitCode::from(2);
        }
    };
    match result {
        Ok(code) => ExitCode::from(code),
        Err(Usage(message)) => {
            eprintln!("chtypes: {message}");
            ExitCode::from(2)
        }
    }
}

/// `docs/fetch.md` §6: 1 verification failed · 3 source unreachable · 4 not
/// published. Everything else that failed is 1 too — it did not succeed.
fn exit_code_for(err: &Error) -> u8 {
    match err.artifact_code() {
        Some(CODE_SOURCE_UNREACHABLE) => 3,
        Some(CODE_ARTIFACT_UNPUBLISHED) => 4,
        _ => 1,
    }
}

fn report(err: &Error) -> u8 {
    match err.artifact_code() {
        Some(code) => eprintln!("{err} [{code}]"),
        None => eprintln!("{err}"),
    }
    exit_code_for(err)
}

fn cmd_fetch(args: &Args) -> Result<u8, Usage> {
    if args.all && !args.lines.is_empty() {
        return Err(Usage(format!(
            "--all installs every published line; drop the version argument ({})",
            args.lines.join(" ")
        )));
    }
    if !args.all && args.lines.is_empty() {
        return Err(Usage("a ClickHouse line is required (or --all)".into()));
    }
    let opts = options(args);
    if args.all {
        return Ok(match fetch::ensure_all(&opts) {
            Ok(installed) => {
                for i in installed {
                    println!("{}", i.dir.display());
                }
                0
            }
            Err(e) => report(&e),
        });
    }
    // Every spelling is checked before the first fetch: a typo in the third
    // argument must not cost the first two downloads.
    for line in &args.lines {
        if let Err(e) = fetch::parse_line(line) {
            return Err(Usage(
                e.to_string()
                    .trim_start_matches("chtypes: fetch: ")
                    .to_string(),
            ));
        }
    }
    for line in &args.lines {
        match fetch::ensure(line, &opts) {
            Ok(i) => println!("{}", i.dir.display()),
            Err(e) => return Ok(report(&e)),
        }
    }
    Ok(0)
}

/// `--dest` names the one directory to look in; without it, the §1 search
/// path — the same rule `fetch` applies to "already installed".
fn dirs_of(args: &Args, opts: &EnsureOptions) -> Result<Vec<PathBuf>, Error> {
    match &args.dest {
        Some(d) => Ok(vec![d.clone()]),
        None => fetch::search_path(opts),
    }
}

fn cmd_verify(args: &Args) -> Result<u8, Usage> {
    let opts = options(args);
    let dirs = match dirs_of(args, &opts) {
        Ok(d) => d,
        Err(e) => return Ok(report(&e)),
    };
    let mut checked = 0usize;
    let mut bad = 0usize;
    for dir in &dirs {
        if !dir.is_dir() {
            continue;
        }
        for v in fetch::verify_installed(dir) {
            checked += 1;
            match &v.result {
                Ok(sha) => println!(
                    "ok       {}  ClickHouse {}  {}  sha256 {sha}",
                    v.dir.display(),
                    v.version,
                    v.library
                        .file_name()
                        .and_then(|n| n.to_str())
                        .unwrap_or("?")
                ),
                Err(e) => {
                    bad += 1;
                    println!("CORRUPT  {}  {e}", v.dir.display());
                }
            }
        }
    }
    if checked == 0 {
        eprintln!(
            "chtypes: nothing is installed under {}",
            dirs.iter()
                .map(|d| d.display().to_string())
                .collect::<Vec<_>>()
                .join(", ")
        );
        return Ok(0);
    }
    eprintln!("chtypes: {checked} installed line(s) re-hashed, {bad} corrupt");
    Ok(if bad == 0 { 0 } else { 1 })
}

fn cmd_list(args: &Args) -> Result<u8, Usage> {
    let opts = options(args);
    let dirs = match dirs_of(args, &opts) {
        Ok(d) => d,
        Err(e) => return Ok(report(&e)),
    };
    let platform = args.platform.clone().unwrap_or_else(host_platform);
    println!("installed ({platform}):");
    let installed = installed_lines(&dirs);
    if installed.is_empty() {
        println!("  (nothing)");
    }
    for (line, dir) in &installed {
        let version = std::fs::read_to_string(dir.join("manifest.json"))
            .ok()
            .and_then(|t| serde_json::from_str::<chtypes::Manifest>(&t).ok())
            .map(|m| m.clickhouse_version)
            .unwrap_or_default();
        println!("  {line:<8} {version:<20} {}", dir.display());
    }
    let quiet = EnsureOptions {
        progress: false,
        ..opts
    };
    match fetch::release_info(&quiet) {
        Ok(info) => {
            println!(
                "release ({}, {}):",
                info.origin,
                match &info.signed_by {
                    Some(key) => format!("signed by ed25519 key {key}"),
                    None => "UNVERIFIED — CHTYPES_ALLOW_UNSIGNED=1".to_string(),
                }
            );
            let mut any = false;
            for row in info.artifacts.iter().filter(|r| r.platform() == platform) {
                any = true;
                let have = installed.iter().any(|(l, _)| l == &row.clickhouse_minor);
                println!(
                    "  {:<8} {:<20} {}  {} bytes{}",
                    row.clickhouse_minor,
                    row.clickhouse_version,
                    row.file,
                    row.bytes,
                    if have { "  (installed)" } else { "" }
                );
            }
            if !any {
                println!("  (nothing for {platform})");
            }
            Ok(0)
        }
        Err(e) => Ok(report(&e)),
    }
}

fn cmd_where(args: &Args) -> Result<u8, Usage> {
    match fetch::install_dir(&options(args)) {
        Ok(dir) => {
            println!("{}", dir.display());
            Ok(0)
        }
        Err(e) => Ok(report(&e)),
    }
}
