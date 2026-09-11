# Security

Report vulnerabilities privately to security@wave-rf.com. Do not open a public issue for a security problem.

What this repository is: the chtypes SDKs. They `dlopen` a native artifact and speak a C ABI to it; they contain no ClickHouse code and no network code of their own. A vulnerability in an artifact (the vendored ClickHouse build) is reported the same way and handled with the core repository.

We respond within three business days and coordinate disclosure with the reporter.
