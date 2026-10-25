# Contributing

hallmark is public and is built next to an adopter's real Databricks environment, so nothing that identifies it may reach a commit, a push or a pull request. The leak gate enforces that.

## Install the hooks

The hooks live in `scripts/githooks/` and call `cq security leak` from the [quenching](https://github.com/holetz/quenching) plugin. Nothing is vendored here. They fail closed: without `cq` on the PATH, or without the private denylist, a commit or push is refused.

```bash
git config core.hooksPath scripts/githooks
```

Nothing in the repository runs this for you; it changes your own clone's git config.

- `pre-commit` checks the staged content and the configured author email.
- `pre-push` checks the added lines, messages, author and committer emails of every commit being pushed.

## The private denylist

The organization-specific terms live outside the repository, at `~/.config/quenching/denylist.txt` (or the file named by `$QUENCHING_DENYLIST_FILE`): one case-insensitive regex per line. Never copy it into the repository, a log, an issue or a commit message. A finding is fixed by removing the content, never by weakening a pattern.

## The repository gate

```bash
bash scripts/verify_repo.sh
```

It checks shell syntax and scans the tracked files for the generic patterns (Brazilian tax ids, Azure Databricks hosts and tokens, cluster ids, private keys). Where `cq` is installed it also runs `scripts/test-leak-gate.sh`, which proves both hooks in a throwaway repository.

## CI

Pull requests run the same gate plus gitleaks over the history (`.github/workflows/ci.yml`). CI has no `cq` and no denylist, so it covers the generic patterns only; the private terms are the hooks' job, which is why installing them matters. Every action is pinned by commit SHA.
