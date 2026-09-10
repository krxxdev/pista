#!/usr/bin/env python3

import argparse
import getpass
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from openpilot.system.hardware import HARDWARE
from openpilot.system.hardware.base import LPABase, LPAError, Profile


MAX_ACTIVATION_CODE_BYTES = 4096
MODEM_STATE_PATH = "/dev/shm/modem"
PROVISIONING_JOURNAL_PATH = "/data/esim-provisioning-state.json"
NOTIFICATION_SNAPSHOT_PATH = "/data/esim-notification-snapshot.json"
ICCID_RE = re.compile(r"^[0-9]{18,22}$")
ICCID_IN_TEXT_RE = re.compile(r"(?<![0-9])[0-9]{18,22}(?![0-9])")
ACTIVATION_IN_TEXT_RE = re.compile(r"LPA:[^\s]+", re.IGNORECASE)

NOT_SUBMITTED = "not_submitted"
RESULT_UNKNOWN = "submission_result_unknown"
INSTALLED_PENDING = "profile_installed_post_processing_pending"
COMPLETE = "complete"
JOURNAL_STATES = {NOT_SUBMITTED, RESULT_UNKNOWN, INSTALLED_PENDING, COMPLETE}


def mask_iccid(iccid: str | None) -> str | None:
  return None if not iccid else f"***{iccid[-4:]}"


def sanitize_error(error: BaseException) -> str:
  message = str(error) if isinstance(error, (LPAError, RuntimeError, ValueError)) else type(error).__name__
  message = ACTIVATION_IN_TEXT_RE.sub("[activation-code-redacted]", message)
  return ICCID_IN_TEXT_RE.sub(lambda match: f"***{match.group(0)[-4:]}", message)


def validate_iccid(iccid: str) -> str:
  value = iccid.strip()
  if not ICCID_RE.fullmatch(value):
    raise ValueError("ICCID must contain 18 to 22 decimal digits")
  return value


def atomic_json_write(path: str, payload: dict[str, Any]) -> None:
  target = Path(path)
  target.parent.mkdir(parents=True, exist_ok=True)
  fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
  try:
    with os.fdopen(fd, "w") as output:
      json.dump(payload, output, sort_keys=True)
      output.write("\n")
      output.flush()
      os.fsync(output.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
  finally:
    try:
      os.unlink(temporary)
    except FileNotFoundError:
      pass


class ProvisioningJournal:
  def __init__(self, path: str = PROVISIONING_JOURNAL_PATH) -> None:
    self.path = path

  def read(self) -> dict[str, Any]:
    try:
      payload = json.loads(Path(self.path).read_text())
    except FileNotFoundError:
      return {"state": NOT_SUBMITTED}
    except (OSError, json.JSONDecodeError):
      raise LPAError("provisioning journal is unreadable; refusing activation-code submission") from None
    if not isinstance(payload, dict) or payload.get("state") not in JOURNAL_STATES:
      raise LPAError("provisioning journal is invalid; refusing activation-code submission")
    return payload

  def assert_submission_allowed(self) -> None:
    state = self.read()["state"]
    if state != NOT_SUBMITTED:
      raise LPAError(f"provisioning journal state is {state}; use read-only reconciliation, not resubmission")

  def write(self, state: str, *, installed_iccid: str | None = None) -> None:
    if state not in JOURNAL_STATES:
      raise ValueError("invalid provisioning journal state")
    payload: dict[str, Any] = {"schema": 1, "state": state, "updated_unix": int(time.time())}
    if installed_iccid:
      payload["installed_iccid"] = validate_iccid(installed_iccid)
    atomic_json_write(self.path, payload)


def read_activation_code() -> str:
  if sys.stdin.isatty():
    raw = getpass.getpass("Activation code: ").encode("utf-8")
  else:
    raw = sys.stdin.buffer.read(MAX_ACTIVATION_CODE_BYTES + 1)
  if not raw or len(raw) > MAX_ACTIVATION_CODE_BYTES or b"\x00" in raw or len(raw.splitlines()) != 1:
    raise LPAError("activation code input is empty, oversized, or malformed")
  try:
    value = raw.decode("utf-8").strip()
  except UnicodeDecodeError:
    raise LPAError("activation code input is not UTF-8") from None
  if not value:
    raise LPAError("activation code input is empty")
  return value


def profile_payload(profile: Profile) -> dict[str, Any]:
  return {"iccid": mask_iccid(profile.iccid), "nickname": profile.nickname, "enabled": profile.enabled,
          "provider": profile.provider, "factory": profile.is_comma}


def read_modem_state(path: str = MODEM_STATE_PATH) -> dict[str, Any]:
  try:
    payload = json.loads(Path(path).read_text())
    return payload if isinstance(payload, dict) else {}
  except (FileNotFoundError, OSError, json.JSONDecodeError):
    return {}


def status_payload(lpa: LPABase) -> dict[str, Any]:
  if hasattr(lpa, "get_profile_state"):
    profiles, active = lpa.get_profile_state()
  else:
    active = lpa.get_active_profile()
    profiles = lpa.list_profiles()
  modem = read_modem_state()
  modem_iccid = str(modem.get("iccid") or "")
  return {
    "evidence": {
      "euicc_active_iccid": mask_iccid(active.iccid),
      "modem_iccid": mask_iccid(modem_iccid),
      "iccids_converged": bool(modem_iccid and modem_iccid == active.iccid),
      "cellular_registration": modem.get("registration", "unknown"),
      "cellular_data_connected": bool(modem.get("connected", False)),
    },
    "active_profile": profile_payload(active),
    "profiles": [profile_payload(profile) for profile in profiles],
  }


def notification_payload(notification: Any) -> dict[str, Any]:
  return {"sequence": notification.sequence, "operation": notification.operation,
          "iccid": mask_iccid(notification.iccid)}


def write_notification_snapshot(notifications: list[Any], target_iccid: str,
                                path: str = NOTIFICATION_SNAPSHOT_PATH) -> None:
  atomic_json_write(path, {"schema": 1, "target_iccid": validate_iccid(target_iccid),
                           "preexisting_sequences": sorted(notification.sequence for notification in notifications),
                           "created_unix": int(time.time())})


def read_notification_snapshot(path: str = NOTIFICATION_SNAPSHOT_PATH) -> dict[str, Any]:
  try:
    payload = json.loads(Path(path).read_text())
  except (FileNotFoundError, OSError, json.JSONDecodeError):
    raise LPAError("notification snapshot is missing or unreadable") from None
  sequences = payload.get("preexisting_sequences") if isinstance(payload, dict) else None
  if (payload.get("schema") != 1 or not isinstance(sequences, list) or
      any(not isinstance(value, int) or value < 0 for value in sequences)):
    raise LPAError("notification snapshot is invalid")
  payload["target_iccid"] = validate_iccid(str(payload.get("target_iccid") or ""))
  return payload


def reconcile_provisioning(lpa: LPABase, journal: ProvisioningJournal) -> dict[str, Any]:
  state = journal.read()
  profiles = lpa.list_profiles()
  installed = state.get("installed_iccid")
  if installed and any(profile.iccid == installed for profile in profiles):
    return {"journal_state": state["state"], "installed_profile": mask_iccid(installed), "installed": True}
  return {"journal_state": state["state"], "installed_profile": mask_iccid(installed), "installed": False,
          "profiles": [profile_payload(profile) for profile in profiles]}


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(prog="esim.py", description="safe comma 4 eSIM operator interface")
  parser.add_argument("--json", action="store_true")
  sub = parser.add_subparsers(dest="command")
  sub.add_parser("list", help="read-only profile list")
  sub.add_parser("active", help="read-only strict active-profile check")
  sub.add_parser("status", help="read-only separated eUICC/modem/data evidence")
  sub.add_parser("notifications", help="read-only sanitized notification metadata")
  sub.add_parser("provision-reconcile", help="read-only provisioning journal reconciliation")
  switch = sub.add_parser("switch", help="explicit reset-prepared single profile enable")
  switch.add_argument("iccid")
  download = sub.add_parser("download", help="one activation-code submission from stdin")
  download.add_argument("--activation-code-stdin", action="store_true", required=True)
  download.add_argument("--nickname", required=True)
  selected = sub.add_parser("notification-send", help="deliver and remove one fresh selected notification")
  selected.add_argument("--sequence", type=int, required=True)
  selected.add_argument("--operation", choices=("install", "enable", "disable", "delete"), required=True)
  selected.add_argument("--iccid", required=True)
  return parser


def emit(payload: Any, json_output: bool) -> None:
  if json_output:
    print(json.dumps(payload, indent=2, sort_keys=True))
  elif isinstance(payload, list):
    for item in payload:
      print(json.dumps(item, sort_keys=True))
  else:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main() -> int:
  parser = build_parser()
  args = parser.parse_args()
  if args.command is None:
    parser.print_help()
    return 0
  lpa = HARDWARE.get_sim_lpa()
  journal = ProvisioningJournal()
  try:
    if args.command == "list":
      payload: Any = [profile_payload(profile) for profile in lpa.list_profiles()]
    elif args.command == "active":
      payload = {"active_profile": profile_payload(lpa.get_active_profile())}
    elif args.command == "status":
      payload = status_payload(lpa)
    elif args.command == "notifications":
      payload = [notification_payload(notification) for notification in lpa.list_notifications()]
    elif args.command == "switch":
      iccid = validate_iccid(args.iccid)
      write_notification_snapshot(lpa.list_notifications(), iccid)
      active = lpa.prepare_and_switch_profile(iccid)
      payload = {"operation": "switch", "mutation_count": 1,
                 "euicc_active_iccid": mask_iccid(active.iccid), "modem_iccid": None,
                 "modem_verification": "not_performed"}
    elif args.command == "download":
      journal.assert_submission_allowed()
      before = {profile.iccid for profile in lpa.list_profiles()}
      activation_code = read_activation_code()
      try:
        try:
          installed_iccid = lpa.download_profile(
            activation_code, args.nickname, on_submission=lambda: journal.write(RESULT_UNKNOWN))
        except Exception:
          if journal.read()["state"] == RESULT_UNKNOWN:
            try:
              after = lpa.list_profiles()
              new_profiles = [profile for profile in after if profile.iccid not in before]
              if len(new_profiles) == 1:
                journal.write(INSTALLED_PENDING, installed_iccid=new_profiles[0].iccid)
            except Exception:
              pass
          raise
      finally:
        activation_code = ""
      journal.write(INSTALLED_PENDING, installed_iccid=installed_iccid)
      payload = {"operation": "download", "journal_state": INSTALLED_PENDING,
                 "installed_profile": mask_iccid(installed_iccid), "preexisting_profile_count": len(before)}
    elif args.command == "provision-reconcile":
      payload = reconcile_provisioning(lpa, journal)
    elif args.command == "notification-send":
      iccid = validate_iccid(args.iccid)
      snapshot = read_notification_snapshot()
      if snapshot["target_iccid"] != iccid:
        raise LPAError("selected ICCID does not match the pre-mutation notification snapshot")
      payload = lpa.process_selected_notification(
        args.sequence, args.operation, iccid, set(snapshot["preexisting_sequences"]))
      payload["iccid"] = mask_iccid(payload["iccid"])
      state = journal.read()
      if state.get("state") == INSTALLED_PENDING and state.get("installed_iccid") == iccid:
        journal.write(COMPLETE, installed_iccid=iccid)
        payload["journal_state"] = COMPLETE
    else:
      raise LPAError("unsupported command")
    emit(payload, args.json)
    return 0
  except Exception as error:
    message = sanitize_error(error)
    if args.json:
      print(json.dumps({"error": message}, sort_keys=True))
    else:
      print(f"error: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
  raise SystemExit(main())
