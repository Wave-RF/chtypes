# close-linked-issues fixtures — captured real API responses

`scripts/close-linked-issues.py --selftest` drives its decision logic from
these files rather than from hand-set expectations, for the two pull
requests chtypes#362 itself measured: #361 (closed #306 by hand after a
policy-merge enqueue left it open) and #357 (a `refs`-only pull request,
which closes nothing). A hand-set `closingIssuesReferences` list would only
ever prove the checker agrees with itself; these were read from the real API
on 2026-10-01, against this repository, with:

```sh
gh api -X GET repos/Wave-RF/chtypes/commits/<merge_commit_sha>/pulls
gh api graphql -f query='
  query($owner:String!,$name:String!,$num:Int!) {
    repository(owner:$owner, name:$name) {
      pullRequest(number:$num) {
        number
        merged
        mergeCommit { oid }
        closingIssuesReferences(first: 50) {
          totalCount
          nodes { number state repository { nameWithOwner } }
        }
      }
    }
  }' -f owner=Wave-RF -f name=chtypes -F num=<361 or 357>
```

`pr-<N>-commits-pulls.json` is the first call's response, trimmed to the
fields `resolve_merged_pr` reads (`number`, `merged_at`,
`merge_commit_sha`); it is what proves a merge commit on main resolves back
to the pull request it merged — `31fd9ca8d6ec66d64cd56ab468d295ced5b3adab`
for #361, `ff670574d5ab756df3d1240f41e52e1e985a741b` for #357, each the real
`merge_commit_sha` GitHub reports for that pull request today.

`pr-<N>-closing-issues.json` is the second call's response, reshaped to the
`{"merged": bool, "closing_issues": [...]}` shape `issues_to_close` takes —
never re-fetched or re-derived by the checker itself. #361's lists #306,
already `CLOSED` (closed by hand after chtypes#362 was noticed), so the
captured-data expectation is "closes nothing" — the same answer a
maintainer-enqueued pull request's already-auto-closed issue would get; #362
does not need a separate maintainer-enqueued case to prove that path, because
`close-linked-issues.py` never reads who enqueued anything. #357's is empty:
a `refs #298` mention is never in `closingIssuesReferences`.

The selftest's one fabricated case (a merged pull request with an `OPEN`,
same-repository reference, expected to close) exists because neither captured
pull request above actually closes anything — without it, nothing would ever
prove `issues_to_close` can return a non-empty list at all.
