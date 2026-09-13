# Draft upstream submission: audited experimental factory eSIM removal workflow

## Summary

This change proposes an offline-plan-first operator/lab tool for one narrow recovery case on comma 4 / MICI: select an already-installed replacement eSIM profile, enable it if needed, and, only with explicit destructive opt-in, remove the explicitly selected disabled factory Webbing profile.

The production LPA protected-profile guard remains unchanged. The bypass is not added to the normal CLI or API. It exists only in a separate experimental tool with exact-target checks, typed offroad checks, dry-run default, durable no-replay journal, shared modem locking, in-channel state reconciliation, masked output, and offline fixture tests.

## Motivation and evidence

In one operator-managed case, an installed Telekom profile registered on LTE, attached, and obtained an IPv4 address and DNS using `IP` plus the carrier APN, but the eUICC returned to Webbing roughly 30 seconds later. The operator subsequently reported deleting the disabled Webbing profile after Telekom was active; later output showed Webbing absent and Telekom enabled. A normal reboot and working data were reported.

Important limitations: the exact successful delete command sequence and radio-enable command were not retained, the new helper is not byte-identical to the transient successful helper, and raw final interface-bound HTTPS/long-duration persistence evidence is incomplete. The new code is therefore offline validated, not hardware validated.

Deleting Webbing is an experimental, potentially irreversible workaround. It does not prove or implement WebbingCTRL Manual mode, alter applet policy, or provide a profile rollback. Maintainers should keep the normal factory-profile guard and decide whether a separate lab-only tool belongs upstream at all.

## Safety properties

- Full, explicit, distinct delete/keep ICCIDs; identifiers are masked in output.
- Replacement must already exist. No activation-code or provisioning path exists.
- Missing or contradictory offroad state fails closed.
- Dry-run consumes synthetic fixtures only and does not import hardware modules.
- No redundant EnableProfile when the replacement is already active.
- Fresh state is checked again inside the owned ISD-R channel immediately before delete.
- Target must be disabled, provider exactly `Webbing`, and `is_comma=true`; replacement must be uniquely active.
- One durable journal reservation precedes each application mutation. Ambiguous results are reconciled and never automatically replayed, including after restart/re-entry.
- Unrelated profiles must remain unchanged.
- No automatic reboot, notification sweep, hidden reset before deletion, preferred-profile guard, or factory reprovisioning.

## Suggested review split

1. Reusable RC2 LPA hardening and diagnostics (existing commits, reviewed independently).
2. Experimental destructive operator helper and offline tests (this candidate).
3. Case study, evidence table, workflow, and limitations (documentation-only).

## Testing

The final submission should include the fresh Python 3.12 and development-Python offline results recorded in the candidate audit. Hardware tests are intentionally absent because device access was prohibited during preparation.
