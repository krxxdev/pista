# comma 4 eSIM factory-profile removal: evidence record

This record reconstructs the 2026-09-10 through 2026-09-13 experiment from local RC2 source, sanitized project notes, and operator reports. It is not a fresh hardware validation.

## Command evidence

| step | exact command/function | precondition | side effect | result | source/time | code version | evidence quality |
|---|---|---|---|---|---|---|---|
| Provision Telekom | Exact activation command intentionally excluded | Explicitly authorized private activation flow | Download/install profile | Telekom profile installed; carrier message confirmed download | Operator handoff, 2026-09-11 | RC2 overlay | OPERATOR_REPORTED |
| Select Telekom | `TiciLPA.prepare_and_switch_profile(<replacement ICCID>)` | Offroad; installed replacement selected by full ICCID | At most one EnableProfile application mutation; RC2 also performs its reset/start preparation | Later reads showed Telekom enabled and Webbing disabled | Local `lpa.py`; operator transcript summary, 2026-09-11 | `453cccd7…` | SOURCE_VERIFIED |
| Configure host APN | `Params().put("GsmApn", "internet.telekom")` and modem initialization `AT+CGDCONT=1,"IP",<apn>` | Telekom selected | Changes host Param and CID 1 configuration | `IP + internet.telekom` observed | Local `modem.py`; operator handoff, 2026-09-11 | device baseline `ce8a50cc…`, RC2 overlay | OPERATOR_REPORTED |
| Verify Telekom radio/PDP | Read-only registration, attach, `QNWINFO`, `CGCONTRDP` queries | Modem ICCID converged to Telekom | None | LTE registration, `CGATT=1`, IPv4 and DNS observed before fallback | Sanitized project diagnostic, 2026-09-11 | RC2 overlay | OBSERVED_COMMAND_OUTPUT |
| Failed lab deletion attempt | Local lab helper printed a DeleteProfile intention before channel acquisition | Telekom shown enabled; Webbing shown disabled | No proven DeleteProfile dispatch | Opening ISD-R failed; print order does not prove the mutation was submitted | Operator transcript summary, 2026-09-11 | RC2 overlay plus transient helper | OBSERVED_COMMAND_OUTPUT |
| Restore radio availability | `UNKNOWN` | Device was offroad | Unknown radio-state change | Operator reported re-enabling the radio | Operator report, after failed ISD-R attempt | Unknown transient command | OPERATOR_REPORTED |
| Delete factory Webbing | Exact successful call sequence `UNKNOWN`; semantically one DeleteProfile for the selected disabled Webbing ICCID | Replacement active; factory target disabled | Irreversible eUICC profile deletion | Operator reported success; later profile list omitted Webbing | Operator report, 2026-09-11/12 | Transient helper, byte identity unavailable | OPERATOR_REPORTED |
| Read final profile list | Exact command not retained | Deletion completed | None | BetterRoaming disabled and Telekom enabled; Webbing absent | Operator-provided output | Unknown transient command | OPERATOR_REPORTED |
| Normal reboot | Exact reboot command not retained | Telekom active after deletion | Device lifecycle restart | Operator showed reboot and reports working cellular data | Operator report | Device baseline plus RC2 | OPERATOR_REPORTED |
| Prove final host cellular route | No qualifying exact command retained | Cellular interface should be up | None | Raw interface-bound HTTPS proof and long-duration persistence evidence are incomplete; pasted Google ping used Wi-Fi | Audit reconstruction | N/A | UNKNOWN |
| Run generalized helper in this candidate | `python3 -m openpilot.system.hardware.tici.tools.webbing_delete_lab ...` | Future explicit operator authorization only | Would enable/delete under gates | Not run on hardware | This commit candidate | RC2 base | PROPOSED_NOT_OBSERVED |

## Evidence boundaries

- The historical successful deletion is not byte-identical hardware validation of the new helper. The new helper is `NEW_OFFLINE_VALIDATED` only.
- The failed transient helper logged its DeleteProfile intention too early. No evidence proves that its mutating APDU was dispatched.
- Removing Webbing prevents fallback to that deleted profile, but does not prove WebbingCTRL Manual mode, a policy update, or applet removal.
- The successful Telekom PDP sample proves the chain through modem PDP and allocated IPv4/DNS for that sample. It does not by itself prove a Linux interface-bound HTTPS request.
- The exact final radio-enable command and complete successful deletion call sequence are missing. They remain missing rather than being reconstructed as `AT+CFUN=1` or another guessed command.
