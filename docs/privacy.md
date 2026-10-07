# Privacy and publication

This repository starts with fresh history and a committed snapshot of the
Claw & Talon startup dossiers. The IP owner authorized Apache 2.0 relicensing
for this repository. Generated rankings, rejected drafts, website assets, deployment settings,
saved runs, provider logs, caches, and uncommitted source edits were excluded.

Research records describe companies and public evidence. The license covers
contributed content, not third-party source pages or trademarks. Source links
and assertions still need human review; generated analysis can be wrong.

## Keep local information local

Authenticate provider CLIs outside this repository. Never put credentials in
`agent.example.toml`; use ignored `agent.toml` for private configuration. Keep
private notes outside `data/startups`, which is deliberately tracked. Run outputs,
indexes, prompts, transcripts, candidate drafts, and cost telemetry belong in the
ignored `.agent-runs` directory. Use ignored `exports/` for local exports.

`discover`, `improve`, and `refresh` pass research context to the selected provider.
That context includes selected existing dossiers and catalog identities. Review
the provider's account settings and tool access before supplying private material.
Staging is not an operating-system sandbox. Offline search, validation, schema,
index, and export commands do not invoke a research provider.

## Review the exact files being committed

```sh
git config --local core.hooksPath .githooks
git status --short
git diff --cached --stat
python3 scripts/check_public_tree.py
git diff --cached
```

The hook and CI inspect indexed blobs, including previously tracked files, so
`git add -f` cannot bypass their sensitive-file rules. They reject binary files,
symlinks, sensitive runtime paths, common credentials, private keys, credential
URLs, and local home paths. Checks print locations and categories, never matched
values. They are heuristic checks and cannot identify every confidential sentence.
Run an independent secret scanner before publishing, and inspect new dossiers
for private notes or nonpublic information. Git ignore rules do not untrack files.

The repository's initial commit uses JJ Ben-Joseph's normal Git identity,
`jj@tensorspace.ai`, at the owner's explicit request. Git author and committer
information is public. The public upstream is
https://github.com/tensorspace-ai/startup-db.
