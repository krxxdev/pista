# Experimental factory Webbing removal workflow

Status: `NEW_OFFLINE_VALIDATED`. This is an operator/lab workflow for a potentially irreversible workaround. It is not a WebbingCTRL Manual-mode implementation.

## Safety model

The replacement profile must already be installed. The workflow selects two distinct full ICCIDs: the factory Webbing profile to delete and the replacement to keep. Output masks both. No activation code, profile download, notification processing, automatic guard, profile backup claim, or reboot is included.

The normal `TiciLPA.delete_profile()` protected-profile refusal remains unchanged. The separate helper uses the same audited ES10 DeleteProfile encoding and shared modem lock only after all of these conditions hold:

- typed `IsOnroad=false` and `IsOffroad=true`;
- exactly one enabled eUICC profile;
- replacement exists exactly once;
- delete target exists exactly once, has provider exactly `Webbing`, and `is_comma=true`;
- replacement is the unique enabled profile and Webbing is disabled immediately before submission;
- unrelated profiles match the initial snapshot;
- explicit `--execute`, exact irreversible confirmation, and a durable private journal are supplied.

Dry-run is the default and accepts a synthetic fixture only. It never constructs the hardware backend.

## Ordered procedure

1. Select the already-installed replacement and factory targets by full ICCID.
2. Run an offline plan from a reviewed synthetic fixture.
3. For a separately authorized future hardware run, verify explicit offroad state and record `GsmApn`.
4. Read profiles through RC2. This exercises the shared lock and an owned ISD-R logical channel.
5. If the replacement is not active, reserve one enable attempt in the journal and call RC2 `prepare_and_switch_profile()` once. An ambiguous attempt is never replayed automatically.
6. Re-read profiles. Require the replacement to be uniquely enabled and Webbing disabled.
7. Re-read the same state inside the lock-held deletion channel. If Webbing reactivated, stop before dispatch.
8. Durably record `delete_submission_may_have_started`, then issue exactly one DeleteProfile application mutation for the selected ICCID.
9. Re-read state. Success requires Webbing absent, replacement active, and unrelated profiles unchanged. An ambiguous response is reconciled with reads and never resent.
10. Verify modem identity and each cellular layer separately. Reboot/stability testing is an optional, separately authorized lifecycle step.

Example offline plan using synthetic identifiers:

```sh
python3 -m openpilot.system.hardware.tici.tools.webbing_delete_lab \
  --delete-iccid 89000000000000000001 \
  --keep-iccid 89000000000000000002 \
  --fixture openpilot/system/hardware/tici/tests/fixtures/webbing_delete_plan.json
```

An execution command is intentionally not populated with real targets. It additionally requires `--execute`, `--confirm-delete DELETE_DISABLED_FACTORY_WEBBING`, and `--journal <private-path>`.

## Mutation and journal semantics

Journal states are `prepared`, `enable_submission_may_have_started`, `enable_response`, `replacement_verified`, `delete_submission_may_have_started`, `delete_response`, and `verified_result`. Application mutation counters are distinct from APDU fragmentation and GET RESPONSE continuation.

A channel-open failure occurs before `delete_submission_may_have_started`; it therefore cannot be reported as a DeleteProfile dispatch. Once that state is durable, restart/re-entry performs read reconciliation only. If the target remains, the tool stops instead of retrying.

The workflow is not atomic. There is no proven profile rollback and no eUICC profile backup. Cleanup errors are bounded by RC2 channel cleanup and do not trigger a hidden modem reset.

## Cellular verification layers

Keep each layer separate:

`profile enabled -> modem ICCID -> LTE registration -> packet attach -> PDP -> host interface -> route -> DNS -> interface-bound cellular HTTPS -> persistence`

The examined baseline uses `/dev/modem_at0` for control, `/dev/modem_at1` for PPP data, `pppd`, and `ppp0`. Do not require `wwan0` or `rmnet_data0`. `GsmApn` and `AT+CGDCONT?` are separate evidence. A Wi-Fi-routed ping or HTTPS request is not cellular proof.
