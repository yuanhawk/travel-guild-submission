# Contributing

## Local secret-scanning hook (required)

This repo ships a `.pre-commit-config.yaml` that runs [gitleaks](https://github.com/gitleaks/gitleaks)
before every commit, using the checked-in `.gitleaks.toml` config (important
here since the frontend bundle is a PUBLIC artifact — e.g. the Google Places
key must never land in source). The config file alone does **not** activate
anything — a fresh clone must install it once:

```bash
pip install pre-commit
pre-commit install
```

After that, `git commit` runs gitleaks automatically and blocks the commit
if it finds a hardcoded secret. To run it manually against the whole tree
(e.g. after first installing, or after pulling changes):

```bash
pre-commit run gitleaks --all-files
```

This showcase repo does not ship a CI workflow (`.github/workflows/`) — the
local pre-commit hook is the only automated secret-scanning gate here.
