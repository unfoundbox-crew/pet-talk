---
title: Fixture Product Roadmap
product: fixture-product
version: 1.0.0
status: living
updated: 2026-09-12
horizon: 2026-Q4
---

## Now (this week)

- [ ] ship the fixture renderer — proves build.py handles every construct — done when: fixtures/out.html builds clean
- [x] write the fixture docs — needed before the renderer can be tested — done when: this file exists

## Next (this month)

- [ ] add a second fixture repo — catches renderer assumptions specific to one repo — done when: two repos build without code changes

## Later (this quarter)

- [ ] add a visual regression check — catches silent CSS breakage — done when: a screenshot diff runs in CI

## Not doing (and why)

- Not building a live-reload dev server: the artifact is a static publish target, not a dev tool.

## Shipped

| Date | Item | Commit/PR |
| --- | --- | --- |
| 2026-09-10 | Format contract written | DOCS-FORMAT.md |

## Decision log

| Date | Decision | Alternatives rejected | Link |
| --- | --- | --- | --- |
| 2026-09-12 | Use a stdlib-only markdown converter | PyYAML + python-markdown (adds a pip dependency) | docs/site/build.py |
