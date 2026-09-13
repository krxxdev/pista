#!/usr/bin/env python3
"""Experimental operator workflow for removing one disabled factory Webbing profile.

Dry-run planning consumes a synthetic fixture and imports no openpilot hardware
module. Real execution is intentionally an explicit, separate lab path; the
normal TiciLPA.delete_profile factory-profile guard remains untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


CONFIRMATION = "DELETE_DISABLED_FACTORY_WEBBING"
ICCID_RE = re.compile(r"^[0-9]{18,22}$")
ICCID_IN_TEXT_RE = re.compile(r"(?<![0-9])[0-9]{18,22}(?![0-9])")
ACTIVATION_IN_TEXT_RE = re.compile(r"LPA:[^\s]+", re.IGNORECASE)
JOURNAL_SCHEMA = 1
JOURNAL_STATES = frozenset({
  "prepared", "enable_submission_may_have_started", "enable_response", "replacement_verified",
  "delete_submission_may_have_started", "delete_response", "verified_result",
})


class WorkflowError(RuntimeError):
  pass


class MutationResultUnknown(WorkflowError):
  pass


@dataclass(frozen=True)
class ProfileView:
  iccid: str
  nickname: str
  enabled: bool
  provider: str
  is_comma: bool

  @classmethod
  def from_object(cls, profile: Any) -> "ProfileView":
    return cls(
      validate_iccid(str(profile.iccid)), str(profile.nickname), bool(profile.enabled),
      str(profile.provider), bool(profile.is_comma),
    )

  @classmethod
  def from_fixture(cls, record: dict[str, Any]) -> "ProfileView":
    required = {"iccid", "nickname", "enabled", "provider", "is_comma"}
    if set(record) != required or not isinstance(record["enabled"], bool) or not isinstance(record["is_comma"], bool):
      raise WorkflowError("fixture profile has an invalid schema")
    return cls(validate_iccid(str(record["iccid"])), str(record["nickname"]), record["enabled"],
               str(record["provider"]), record["is_comma"])


class Backend(Protocol):
  def read_host_state(self) -> dict[str, Any]: ...
  def list_profiles(self) -> list[ProfileView]: ...
  def enable_replacement(self, iccid: str) -> None: ...
  def delete_factory_webbing_once(self, target_iccid: str, verify: Callable[[list[ProfileView]], None],
                                  on_submission: Callable[[], None]) -> int: ...


def validate_iccid(value: str) -> str:
  if not ICCID_RE.fullmatch(value):
    raise WorkflowError("ICCID must contain 18 to 22 decimal digits")
  return value


def mask_iccid(value: str) -> str:
  return f"***{value[-4:]}"


def sanitize_error(error: BaseException) -> str:
  text = str(error) if isinstance(error, (WorkflowError, RuntimeError, ValueError)) else type(error).__name__
  text = ACTIVATION_IN_TEXT_RE.sub("[activation-code-redacted]", text)
  return ICCID_IN_TEXT_RE.sub(lambda match: mask_iccid(match.group(0)), text)


def require_offroad(host_state: dict[str, Any]) -> None:
  onroad = host_state.get("IsOnroad")
  offroad = host_state.get("IsOffroad")
  if onroad is not False or offroad is not True:
    raise WorkflowError("mutation requires typed IsOnroad=false and IsOffroad=true")


def require_distinct_targets(delete_iccid: str, keep_iccid: str) -> None:
  if delete_iccid == keep_iccid:
    raise WorkflowError("delete and keep ICCIDs must be distinct")


@dataclass(frozen=True)
class CheckedState:
  profiles: tuple[ProfileView, ...]
  active: ProfileView
  target: ProfileView | None
  replacement: ProfileView


def check_state(profiles: list[ProfileView], delete_iccid: str, keep_iccid: str,
                *, target_may_be_absent: bool = False, target_must_be_disabled: bool = False) -> CheckedState:
  if len({profile.iccid for profile in profiles}) != len(profiles):
    raise WorkflowError("profile list contains duplicate ICCIDs")
  matches = [profile for profile in profiles if profile.iccid == keep_iccid]
  if len(matches) != 1:
    raise WorkflowError(f"expected exactly one replacement profile {mask_iccid(keep_iccid)}; observed {len(matches)}")
  active = [profile for profile in profiles if profile.enabled]
  if len(active) != 1:
    raise WorkflowError(f"expected exactly one enabled profile; observed {len(active)}")
  target_matches = [profile for profile in profiles if profile.iccid == delete_iccid]
  if len(target_matches) > 1 or (not target_matches and not target_may_be_absent):
    raise WorkflowError(f"expected exactly one delete target {mask_iccid(delete_iccid)}; observed {len(target_matches)}")
  target = target_matches[0] if target_matches else None
  if target is not None:
    if target.provider != "Webbing" or not target.is_comma:
      raise WorkflowError("delete target is not the exact factory Webbing profile")
    if target_must_be_disabled and target.enabled:
      raise WorkflowError("factory Webbing target became enabled; deletion was not submitted")
  return CheckedState(tuple(profiles), active[0], target, matches[0])


def unrelated_snapshot(profiles: tuple[ProfileView, ...], delete_iccid: str, keep_iccid: str) -> dict[str, ProfileView]:
  return {profile.iccid: profile for profile in profiles if profile.iccid not in (delete_iccid, keep_iccid)}


def require_unrelated_unchanged(expected: dict[str, ProfileView], profiles: tuple[ProfileView, ...],
                                delete_iccid: str, keep_iccid: str) -> None:
  observed = unrelated_snapshot(profiles, delete_iccid, keep_iccid)
  if observed != expected:
    raise WorkflowError("unrelated eUICC profiles changed during the workflow")


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
  try:
    with os.fdopen(fd, "w") as output:
      json.dump(payload, output, sort_keys=True)
      output.write("\n")
      output.flush()
      os.fsync(output.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
      os.fsync(directory_fd)
    finally:
      os.close(directory_fd)
  finally:
    try:
      os.unlink(temporary)
    except FileNotFoundError:
      pass


class MutationJournal:
  def __init__(self, path: Path, delete_iccid: str, keep_iccid: str) -> None:
    self.path = path
    self.delete_iccid = delete_iccid
    self.keep_iccid = keep_iccid
    self.payload = self._load_or_create()

  def _load_or_create(self) -> dict[str, Any]:
    try:
      payload = json.loads(self.path.read_text())
    except FileNotFoundError:
      payload = {
        "schema": JOURNAL_SCHEMA, "state": "prepared", "delete_iccid": self.delete_iccid,
        "keep_iccid": self.keep_iccid, "enable_attempts": 0, "delete_attempts": 0,
      }
      atomic_json_write(self.path, payload)
      return payload
    except (OSError, json.JSONDecodeError):
      raise WorkflowError("operator journal is unreadable; refusing mutation") from None
    if (not isinstance(payload, dict) or payload.get("schema") != JOURNAL_SCHEMA or
        payload.get("delete_iccid") != self.delete_iccid or payload.get("keep_iccid") != self.keep_iccid or
        payload.get("state") not in JOURNAL_STATES or
        not isinstance(payload.get("enable_attempts"), int) or payload["enable_attempts"] < 0 or
        not isinstance(payload.get("delete_attempts"), int) or payload["delete_attempts"] < 0):
      raise WorkflowError("operator journal is invalid or belongs to different targets")
    if ((payload["enable_attempts"] and payload["state"] == "prepared") or
        (payload["delete_attempts"] and payload["state"] not in
         ("delete_submission_may_have_started", "delete_response", "verified_result"))):
      raise WorkflowError("operator journal counters contradict its state")
    return payload

  def record(self, state: str, **fields: Any) -> None:
    self.payload = {**self.payload, **fields, "state": state, "updated_unix": int(time.time())}
    atomic_json_write(self.path, self.payload)


def masked_profiles(profiles: tuple[ProfileView, ...]) -> list[dict[str, Any]]:
  return [{"iccid": mask_iccid(profile.iccid), "nickname": profile.nickname, "enabled": profile.enabled,
           "provider": profile.provider, "factory": profile.is_comma} for profile in profiles]


def plan_fixture(path: Path, delete_iccid: str, keep_iccid: str) -> dict[str, Any]:
  try:
    payload = json.loads(path.read_text())
  except (OSError, json.JSONDecodeError):
    raise WorkflowError("offline fixture is unreadable") from None
  if not isinstance(payload, dict) or not isinstance(payload.get("host_state"), dict) or not isinstance(payload.get("profiles"), list):
    raise WorkflowError("offline fixture has an invalid schema")
  require_offroad(payload["host_state"])
  profiles = [ProfileView.from_fixture(record) for record in payload["profiles"]]
  checked = check_state(profiles, delete_iccid, keep_iccid, target_may_be_absent=True)
  if checked.target is None:
    if checked.active.iccid != keep_iccid:
      raise WorkflowError("delete target is absent but replacement is not active")
    actions: list[str] = []
    result = "already_complete"
  else:
    actions = [] if checked.active.iccid == keep_iccid else ["enable_replacement_once"]
    actions.append("fresh_in_channel_state_check")
    actions.append("delete_disabled_factory_webbing_once")
    actions.append("verify_result")
    result = "prepared"
  return {"mode": "offline_plan", "result": result, "actions": actions,
          "delete_target": mask_iccid(delete_iccid), "keep_target": mask_iccid(keep_iccid),
          "profiles": masked_profiles(checked.profiles), "profile_mutations": 0}


def _journal_record_preserving(error: BaseException, journal: MutationJournal, state: str) -> BaseException:
  try:
    journal.record(state, primary_error=sanitize_error(error))
  except Exception as journal_error:
    setattr(error, "journal_error", sanitize_error(journal_error))
  return error


def execute_workflow(backend: Backend, delete_iccid: str, keep_iccid: str,
                     journal: MutationJournal) -> dict[str, Any]:
  host_state = backend.read_host_state()
  require_offroad(host_state)
  initial = check_state(backend.list_profiles(), delete_iccid, keep_iccid, target_may_be_absent=True)
  unrelated = unrelated_snapshot(initial.profiles, delete_iccid, keep_iccid)

  if initial.target is None:
    if initial.active.iccid != keep_iccid:
      raise WorkflowError("delete target is absent but replacement is not active")
    require_unrelated_unchanged(unrelated, initial.profiles, delete_iccid, keep_iccid)
    journal.record("verified_result", result="already_complete")
    return _result(initial, journal, host_state, "already_complete")

  state = str(journal.payload.get("state"))
  if state == "verified_result":
    raise WorkflowError("journal says verified_result but the delete target is still present")
  if state in ("delete_submission_may_have_started", "delete_response"):
    current = check_state(backend.list_profiles(), delete_iccid, keep_iccid, target_may_be_absent=True)
    if current.target is None and current.active.iccid == keep_iccid:
      require_unrelated_unchanged(unrelated, current.profiles, delete_iccid, keep_iccid)
      journal.record("verified_result", result="deleted_after_reconciliation")
      return _result(current, journal, host_state, "deleted_after_reconciliation")
    raise MutationResultUnknown("a prior DeleteProfile may have started; target remains present and will not be retried")

  if state in ("enable_submission_may_have_started", "enable_response"):
    current = check_state(backend.list_profiles(), delete_iccid, keep_iccid)
    if current.active.iccid != keep_iccid or current.target is None or current.target.enabled:
      raise MutationResultUnknown("a prior EnableProfile may have started; replacement state is not verified and will not be retried")
    journal.record("replacement_verified")
    initial = current

  if initial.active.iccid != keep_iccid:
    journal.record("enable_submission_may_have_started", enable_attempts=journal.payload["enable_attempts"] + 1)
    try:
      backend.enable_replacement(keep_iccid)
      journal.record("enable_response")
    except Exception as error:
      try:
        observed = check_state(backend.list_profiles(), delete_iccid, keep_iccid)
      except Exception:
        raise _journal_record_preserving(error, journal, "enable_submission_may_have_started")
      if observed.active.iccid != keep_iccid or observed.target is None or observed.target.enabled:
        raise _journal_record_preserving(error, journal, "enable_submission_may_have_started")
    initial = check_state(backend.list_profiles(), delete_iccid, keep_iccid, target_must_be_disabled=True)
    require_unrelated_unchanged(unrelated, initial.profiles, delete_iccid, keep_iccid)

  if initial.target is None or initial.active.iccid != keep_iccid or initial.target.enabled:
    raise WorkflowError("replacement/delete-target preconditions are not satisfied")
  journal.record("replacement_verified")

  def verify_immediately_before_delete(profiles: list[ProfileView]) -> None:
    require_offroad(backend.read_host_state())
    current = check_state(profiles, delete_iccid, keep_iccid, target_must_be_disabled=True)
    if current.active.iccid != keep_iccid:
      raise WorkflowError("replacement is not the unique enabled profile; deletion was not submitted")
    require_unrelated_unchanged(unrelated, current.profiles, delete_iccid, keep_iccid)

  def mark_delete_submission() -> None:
    journal.record("delete_submission_may_have_started", delete_attempts=journal.payload["delete_attempts"] + 1)

  try:
    code = backend.delete_factory_webbing_once(delete_iccid, verify_immediately_before_delete, mark_delete_submission)
    journal.record("delete_response", delete_status=code)
    if code != 0:
      raise WorkflowError(f"DeleteProfile returned status 0x{code:02X}; mutation was not retried")
  except Exception as error:
    try:
      observed = check_state(backend.list_profiles(), delete_iccid, keep_iccid, target_may_be_absent=True)
    except Exception:
      raise _journal_record_preserving(error, journal, str(journal.payload.get("state")))
    if observed.target is not None:
      raise _journal_record_preserving(error, journal, str(journal.payload.get("state")))
    initial = observed
  else:
    initial = check_state(backend.list_profiles(), delete_iccid, keep_iccid, target_may_be_absent=True)

  if initial.target is not None or initial.active.iccid != keep_iccid:
    raise MutationResultUnknown("post-delete read did not verify the requested final profile state")
  require_unrelated_unchanged(unrelated, initial.profiles, delete_iccid, keep_iccid)
  journal.record("verified_result", result="deleted")
  return _result(initial, journal, host_state, "deleted")


def _result(state: CheckedState, journal: MutationJournal, host_state: dict[str, Any], result: str) -> dict[str, Any]:
  return {"mode": "execute", "result": result, "active_profile": mask_iccid(state.active.iccid),
          "profiles": masked_profiles(state.profiles), "GsmApn": host_state.get("GsmApn", "unknown"),
          "enable_profile_attempts": journal.payload["enable_attempts"],
          "delete_profile_attempts": journal.payload["delete_attempts"], "journal_state": journal.payload["state"]}


class TiciRuntimeBackend:
  """Thin adapter around the RC2 TiciLPA internals; constructed only for --execute."""

  def __init__(self) -> None:
    from openpilot.common.params import Params
    from openpilot.system.hardware import HARDWARE
    from openpilot.system.hardware.tici import lpa as tici_lpa

    self.params = Params()
    self.lpa = HARDWARE.get_sim_lpa()
    self.module = tici_lpa
    required = ("_deadline", "_acquire_channel", "_client", "prepare_and_switch_profile")
    if any(not hasattr(self.lpa, name) for name in required):
      raise WorkflowError("runtime LPA is not the audited RC2 TiciLPA interface")

  @staticmethod
  def _param_bool(value: Any) -> Any:
    if isinstance(value, bytes):
      value = value.decode("ascii", errors="strict")
    if value in (True, "1"):
      return True
    if value in (False, "0"):
      return False
    return value

  def read_host_state(self) -> dict[str, Any]:
    apn = self.params.get("GsmApn", encoding="utf8")
    return {"IsOnroad": self._param_bool(self.params.get("IsOnroad")),
            "IsOffroad": self._param_bool(self.params.get("IsOffroad")),
            "GsmApn": apn if isinstance(apn, str) else "unknown"}

  def list_profiles(self) -> list[ProfileView]:
    return [ProfileView.from_object(profile) for profile in self.lpa.list_profiles()]

  def enable_replacement(self, iccid: str) -> None:
    self.lpa.prepare_and_switch_profile(iccid)

  def delete_factory_webbing_once(self, target_iccid: str, verify: Callable[[list[ProfileView]], None],
                                  on_submission: Callable[[], None]) -> int:
    deadline = self.lpa._deadline()
    with self.lpa._acquire_channel(deadline):
      profiles = self.module.validate_profiles(self.module.list_profiles(self.lpa._client, deadline))
      verify([ProfileView.from_object(profile) for profile in profiles])
      on_submission()
      request = self.module.encode_tlv(
        self.module.TAG_DELETE_PROFILE,
        self.module.encode_tlv(self.module.TAG_ICCID, self.module.string_to_tbcd(target_iccid)),
      )
      response = self.module.es10x_command(self.lpa._client, request, mutating=True, deadline=deadline)
      return self.module.require_tag(
        self.module.require_tag(response, self.module.TAG_DELETE_PROFILE, "DeleteProfileResponse"),
        self.module.TAG_STATUS, "DeleteProfile status",
      )[0]


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description="offline-plan-first experimental factory Webbing removal workflow")
  parser.add_argument("--delete-iccid", required=True, help="full ICCID of the factory Webbing profile")
  parser.add_argument("--keep-iccid", required=True, help="full ICCID of the already-installed replacement profile")
  parser.add_argument("--fixture", type=Path, help="synthetic JSON fixture for the default offline plan")
  parser.add_argument("--execute", action="store_true", help="use the real audited RC2 TiciLPA runtime")
  parser.add_argument("--confirm-delete", help=f"required exact value for execution: {CONFIRMATION}")
  parser.add_argument("--journal", type=Path, help="durable private operation journal required for execution")
  return parser


def main(argv: list[str] | None = None, *, backend_factory: Callable[[], Backend] = TiciRuntimeBackend) -> int:
  args = build_parser().parse_args(argv)
  try:
    delete_iccid = validate_iccid(args.delete_iccid)
    keep_iccid = validate_iccid(args.keep_iccid)
    require_distinct_targets(delete_iccid, keep_iccid)
    if not args.execute:
      if args.fixture is None:
        raise WorkflowError("offline plan requires --fixture; hardware is never inferred")
      result = plan_fixture(args.fixture, delete_iccid, keep_iccid)
    else:
      if args.fixture is not None:
        raise WorkflowError("--fixture and --execute are mutually exclusive")
      if args.confirm_delete != CONFIRMATION:
        raise WorkflowError("exact irreversible-delete confirmation is required")
      if args.journal is None:
        raise WorkflowError("--execute requires a durable --journal path")
      journal = MutationJournal(args.journal, delete_iccid, keep_iccid)
      result = execute_workflow(backend_factory(), delete_iccid, keep_iccid, journal)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0
  except Exception as error:
    payload = {"error": sanitize_error(error)}
    if hasattr(error, "journal_error"):
      payload["journal_error"] = getattr(error, "journal_error")
    print(json.dumps(payload, sort_keys=True))
    return 1


if __name__ == "__main__":
  raise SystemExit(main())
