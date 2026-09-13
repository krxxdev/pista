# Commit candidate plan

Publication base: historical SunnyPilot base `18406e77ee519f40cf12d7f260876ef007dd4b4f`, with RC1 `7c2d5a48…` and RC2 `453cccd7…` retained as separate existing commits. Upstream applicability is not freshly verified because this preparation is strictly offline.

## Proposed split

1. `esim: add offline-gated factory Webbing removal lab tool`
   Includes the experimental helper, value-suppressing scanner, synthetic fixture, and offline tests. The normal LPA guard is unchanged.
2. `docs: record factory eSIM removal workflow and evidence limits`
   Includes the evidence table, English workflow, Hungarian lessons, upstream draft, secret audit, and this plan.
3. Optional existing-history review only: retain RC1/RC2 reusable LPA work as their own commits; do not squash in dormant lpac/QMI/QRTR, profile guards, GP/WBG probes, deployment backups, or personal release archives.

## Explicit manifest

Use the adjacent `ESIM-WEBBING-REMOVAL-COMMIT-MANIFEST.txt`. Never use `git add .` or `git add -A`. The existing index was empty at preparation start and must remain untouched until operator review.

No commit, push, PR, deploy, hardware test, or history rewrite is part of this preparation.
