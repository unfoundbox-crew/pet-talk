# Archie — vendored from AgentWorth

Plain vendored copy, same pattern as `design/sync-agentworth.sh` for
`tokens.css`: pet-talk does not depend on AgentWorth as an npm package
(`packages/ui` has no `package.json` to depend on), so these files are a
one-time `cp`, not a symlink or a build-time fetch.

Source: `/Users/saurabh/code/unfoundbox/agentworth/packages/ui/brand/archie/`
(read `agentworth/docs/DESIGN.md` section "Archie" and this folder's own
README there before touching any of this).

Vendored as of 2026-09-12: `front-sit.svg`, `three-quarter.svg`,
`side-scent.svg`, `digging.svg`, `fetching.svg`, `dropping.svg`,
`sleeping.svg`, `error.svg`, `archie.css`. The `-dark` sibling files were
skipped — nothing in pet-talk renders Archie outside a document that can see
CSS custom properties, so the fixed-fill variants have no consumer here.

Do not edit these files by hand. To re-sync after AgentWorth changes the
mascot:

```bash
SRC=/Users/saurabh/code/unfoundbox/agentworth/packages/ui/brand/archie
DEST=web/src/brand/archie
for f in front-sit three-quarter side-scent digging fetching dropping sleeping error; do
  cp "$SRC/$f.svg" "$DEST/$f.svg"
done
cp "$SRC/archie.css" "$DEST/archie.css"
```

`../Archie.tsx` is pet-talk's port of `packages/ui/Archie.tsx` (same props,
same `data-size="small"` rule below 40px, same C3-default/C4-available
colourways) — its import paths point at the files in this folder instead of
AgentWorth's own tree. `../../components/ArchieGlyph.tsx` is pet-talk's own
addition: the notch/menu-bar glyph, always forced to the small form, with
its state (idle/listening/speaking/error) driven by the same `data-lamp`
convention these SVGs already carry.
