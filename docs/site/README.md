# docs/site — living-docs renderer

Turns a repo's `docs/ARCHITECTURE.md` + `docs/ROADMAP.md` into one
self-contained HTML page, styled for publishing as a claude.ai Artifact.
Python 3 stdlib only — no pip install, nothing to vendor but this directory.

## Vendor into another repo

Copy the whole `docs/site/` directory (`build.py`, `template.html`,
`README.md`; skip `fixtures/`) into the target repo at the same path.
Nothing else to install or configure.

## Rebuild

```bash
python3 docs/site/build.py \
  --arch docs/ARCHITECTURE.md \
  --roadmap docs/ROADMAP.md \
  --out docs/site/index.html \
  --product-name "Your Product" \
  --repo-url "https://github.com/org/repo"
```

Both source docs must follow the format contract (frontmatter keys, exact
`## ` section titles) — see `DOCS-FORMAT.md`. The build doubles as the
format lint: a missing key or section exits non-zero with the exact list
of what's missing, nothing gets written.

Publish `docs/site/index.html` with the Artifact tool (`file_path`, no
`url`) — it already starts with `<title>`/`<style>`, no `<html>`/`<body>`.

To re-test the renderer itself, rebuild the fixtures:

```bash
python3 docs/site/build.py --arch docs/site/fixtures/ARCHITECTURE.md \
  --roadmap docs/site/fixtures/ROADMAP.md --out docs/site/fixtures/out.html
```
