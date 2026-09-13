import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
TOOL_PATH = ROOT / "system/hardware/tici/tools/webbing_delete_lab.py"
SCANNER_PATH = ROOT / "system/hardware/tici/tools/scan_esim_candidate.py"
FIXTURE = Path(__file__).parent / "fixtures/webbing_delete_plan.json"


def load(name, path):
  spec = importlib.util.spec_from_file_location(name, path)
  module = importlib.util.module_from_spec(spec)
  sys.modules[name] = module
  spec.loader.exec_module(module)
  return module


lab = load("webbing_delete_lab_under_test", TOOL_PATH)
scanner = load("scan_esim_candidate_under_test", SCANNER_PATH)

WEBBING = "89000000000000000001"
KEEP = "89000000000000000002"
OTHER = "89000000000000000003"


def profile(iccid, enabled, provider="Synthetic", nickname="test", is_comma=False):
  return lab.ProfileView(iccid, nickname, enabled, provider, is_comma)


class FakeBackend:
  def __init__(self, profiles=None):
    self.profiles = profiles or [profile(WEBBING, True, "Webbing", "factory", True), profile(KEEP, False)]
    self.enable_calls = 0
    self.delete_calls = 0
    self.channel_failure = False
    self.reactivate_before_delete = False
    self.delete_ambiguous = False
    self.enable_ambiguous = False
    self.cleanup_error = False

  def read_host_state(self):
    return {"IsOnroad": False, "IsOffroad": True, "GsmApn": "carrier.example.invalid"}

  def list_profiles(self):
    return list(self.profiles)

  def enable_replacement(self, iccid):
    self.enable_calls += 1
    if self.enable_ambiguous:
      raise RuntimeError("synthetic ambiguous enable")
    self.profiles = [lab.ProfileView(p.iccid, p.nickname, p.iccid == iccid, p.provider, p.is_comma) for p in self.profiles]

  def delete_factory_webbing_once(self, target_iccid, verify, on_submission):
    if self.channel_failure:
      raise RuntimeError("synthetic channel open failure")
    if self.reactivate_before_delete:
      self.profiles = [lab.ProfileView(p.iccid, p.nickname, p.iccid == target_iccid, p.provider, p.is_comma)
                       for p in self.profiles]
    verify(self.list_profiles())
    on_submission()
    self.delete_calls += 1
    if self.delete_ambiguous:
      raise RuntimeError("synthetic ambiguous transport")
    self.profiles = [p for p in self.profiles if p.iccid != target_iccid]
    if self.cleanup_error:
      raise RuntimeError("synthetic cleanup failure after response")
    return 0


class LabTests(unittest.TestCase):
  def setUp(self):
    self.tempdir = tempfile.TemporaryDirectory()
    self.addCleanup(self.tempdir.cleanup)
    self.journal_path = Path(self.tempdir.name) / "journal.json"

  def journal(self):
    return lab.MutationJournal(self.journal_path, WEBBING, KEEP)

  def execute(self, backend):
    return lab.execute_workflow(backend, WEBBING, KEEP, self.journal())

  def test_import_help_and_plan_do_not_construct_runtime_backend(self):
    calls = []
    for argv in (["--help"], ["--delete-iccid", WEBBING, "--keep-iccid", KEEP, "--fixture", str(FIXTURE)]):
      output = io.StringIO()
      with contextlib.redirect_stdout(output):
        try:
          rc = lab.main(argv, backend_factory=lambda: calls.append(True))
        except SystemExit as error:
          rc = error.code
      self.assertEqual(rc, 0)
    self.assertEqual(calls, [])

  def test_no_confirmation_means_zero_mutation_and_no_backend(self):
    calls = []
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
      rc = lab.main(["--delete-iccid", WEBBING, "--keep-iccid", KEEP, "--execute",
                     "--journal", str(self.journal_path)], backend_factory=lambda: calls.append(True))
    self.assertEqual(rc, 1)
    self.assertEqual(calls, [])

  def test_invalid_states_fail_before_mutation(self):
    cases = [
      [profile(WEBBING, True, "Webbing", is_comma=True)],
      [profile(WEBBING, True, "Webbing", is_comma=True), profile(KEEP, True)],
      [profile(WEBBING, True, "Not Webbing", is_comma=True), profile(KEEP, False)],
      [profile(WEBBING, True, "Webbing", is_comma=True), profile(WEBBING, False, "Webbing", is_comma=True),
       profile(KEEP, False)],
    ]
    for profiles in cases:
      with self.subTest(profiles=profiles):
        backend = FakeBackend(profiles)
        with self.assertRaises(lab.WorkflowError):
          self.execute(backend)
        self.assertEqual((backend.enable_calls, backend.delete_calls), (0, 0))
        self.journal_path.unlink(missing_ok=True)
    with self.assertRaises(lab.WorkflowError):
      lab.require_distinct_targets(WEBBING, WEBBING)

  def test_invalid_offroad_fails_before_profile_read(self):
    backend = FakeBackend()
    backend.read_host_state = lambda: {"IsOnroad": False, "IsOffroad": False}
    with self.assertRaises(lab.WorkflowError):
      self.execute(backend)
    self.assertEqual((backend.enable_calls, backend.delete_calls), (0, 0))

  def test_factory_missing_replacement_active_is_noop(self):
    backend = FakeBackend([profile(KEEP, True), profile(OTHER, False)])
    result = self.execute(backend)
    self.assertEqual(result["result"], "already_complete")
    self.assertEqual((backend.enable_calls, backend.delete_calls), (0, 0))

  def test_replacement_already_active_has_no_redundant_enable(self):
    backend = FakeBackend([profile(WEBBING, False, "Webbing", is_comma=True), profile(KEEP, True)])
    result = self.execute(backend)
    self.assertEqual(result["result"], "deleted")
    self.assertEqual((backend.enable_calls, backend.delete_calls), (0, 1))

  def test_channel_open_failure_has_no_delete_dispatch_claim(self):
    backend = FakeBackend([profile(WEBBING, False, "Webbing", is_comma=True), profile(KEEP, True)])
    backend.channel_failure = True
    with self.assertRaisesRegex(RuntimeError, "channel open"):
      self.execute(backend)
    self.assertEqual(backend.delete_calls, 0)
    payload = json.loads(self.journal_path.read_text())
    self.assertEqual(payload["delete_attempts"], 0)
    self.assertEqual(payload["state"], "replacement_verified")

  def test_webbing_reactivation_before_delete_stops(self):
    backend = FakeBackend([profile(WEBBING, False, "Webbing", is_comma=True), profile(KEEP, True)])
    backend.reactivate_before_delete = True
    with self.assertRaisesRegex(lab.WorkflowError, "became enabled"):
      self.execute(backend)
    self.assertEqual(backend.delete_calls, 0)
    self.assertEqual(json.loads(self.journal_path.read_text())["delete_attempts"], 0)

  def test_offroad_change_inside_delete_channel_stops(self):
    backend = FakeBackend([profile(WEBBING, False, "Webbing", is_comma=True), profile(KEEP, True)])
    states = iter(({"IsOnroad": False, "IsOffroad": True}, {"IsOnroad": True, "IsOffroad": False}))
    backend.read_host_state = lambda: next(states)
    with self.assertRaisesRegex(lab.WorkflowError, "[Oo]ffroad"):
      self.execute(backend)
    self.assertEqual(backend.delete_calls, 0)
    self.assertEqual(json.loads(self.journal_path.read_text())["delete_attempts"], 0)

  def test_switch_then_one_delete_and_unrelated_unchanged(self):
    unrelated = profile(OTHER, False, "Synthetic Other", "untouched")
    backend = FakeBackend([profile(WEBBING, True, "Webbing", is_comma=True), profile(KEEP, False), unrelated])
    result = self.execute(backend)
    self.assertEqual(result["result"], "deleted")
    self.assertEqual((backend.enable_calls, backend.delete_calls), (1, 1))
    self.assertIn(unrelated, backend.profiles)

  def test_ambiguous_delete_is_not_replayed_on_reentry(self):
    backend = FakeBackend([profile(WEBBING, False, "Webbing", is_comma=True), profile(KEEP, True)])
    backend.delete_ambiguous = True
    with self.assertRaisesRegex(RuntimeError, "ambiguous"):
      self.execute(backend)
    self.assertEqual(backend.delete_calls, 1)
    with self.assertRaises(lab.MutationResultUnknown):
      lab.execute_workflow(backend, WEBBING, KEEP, self.journal())
    self.assertEqual(backend.delete_calls, 1)

  def test_ambiguous_enable_is_not_replayed_on_reentry(self):
    backend = FakeBackend()
    backend.enable_ambiguous = True
    with self.assertRaisesRegex(RuntimeError, "ambiguous enable"):
      self.execute(backend)
    self.assertEqual(backend.enable_calls, 1)
    with self.assertRaises(lab.MutationResultUnknown):
      lab.execute_workflow(backend, WEBBING, KEEP, self.journal())
    self.assertEqual(backend.enable_calls, 1)

  def test_verified_journal_with_present_target_fails_closed(self):
    backend = FakeBackend([profile(WEBBING, False, "Webbing", is_comma=True), profile(KEEP, True)])
    journal = self.journal()
    journal.record("verified_result", result="synthetic")
    with self.assertRaisesRegex(lab.WorkflowError, "still present"):
      lab.execute_workflow(backend, WEBBING, KEEP, journal)
    self.assertEqual((backend.enable_calls, backend.delete_calls), (0, 0))

  def test_ambiguous_delete_reconciles_absent_target(self):
    backend = FakeBackend([profile(WEBBING, False, "Webbing", is_comma=True), profile(KEEP, True)])
    def ambiguous_after_apply(target, verify, on_submission):
      verify(backend.list_profiles())
      on_submission()
      backend.delete_calls += 1
      backend.profiles = [p for p in backend.profiles if p.iccid != target]
      raise RuntimeError("synthetic lost response")
    backend.delete_factory_webbing_once = ambiguous_after_apply
    result = self.execute(backend)
    self.assertEqual(result["result"], "deleted")
    self.assertEqual(backend.delete_calls, 1)

  def test_cleanup_error_does_not_hide_verified_result(self):
    backend = FakeBackend([profile(WEBBING, False, "Webbing", is_comma=True), profile(KEEP, True)])
    backend.cleanup_error = True
    result = self.execute(backend)
    self.assertEqual(result["result"], "deleted")

  def test_normal_factory_delete_guard_remains_in_source(self):
    source = (ROOT / "system/hardware/tici/lpa.py").read_text()
    self.assertIn("if is_protected_profile(profile):", source)
    self.assertIn("refusing to delete protected profile", source)

  def test_sanitization_masks_iccids(self):
    self.assertNotIn(WEBBING, lab.sanitize_error(RuntimeError(f"target {WEBBING}")))
    self.assertIn("***0001", lab.sanitize_error(RuntimeError(f"target {WEBBING}")))
    self.assertNotIn("matching", lab.sanitize_error(RuntimeError("LPA:1$smdp.example.invalid$matching")))


class SecretScannerTests(unittest.TestCase):
  def test_exact_and_url_encoded_values_are_counted_without_emission(self):
    with tempfile.TemporaryDirectory() as directory:
      secret = Path(directory) / "private.txt"
      secret.write_text("LPA:" + "1$private.example$SYNTHETIC-PRIVATE\n")
      exact = scanner.secret_variants(secret)
      data = b"prefix LPA%3A" + b"1%24private.example%24SYNTHETIC-PRIVATE suffix"
      findings = scanner.scan_bytes(data, exact)
      self.assertGreater(findings["url_decoded_private_value"], 0)
      rendered = json.dumps(findings)
      self.assertNotIn("SYNTHETIC-PRIVATE", rendered)

  def test_invalid_domain_fixture_is_not_live_activation(self):
    findings = scanner.scan_bytes(b"LPA:1$smdp.example.invalid$SYNTHETIC", ())
    self.assertEqual(findings["live_activation_shape"], 0)
    findings = scanner.scan_bytes(b"LPA:" + b"1$smdp.carrier.hu$SYNTHETIC", ())
    self.assertEqual(findings["live_activation_shape"], 1)

  def test_help_has_no_git_or_network_side_effect(self):
    result = subprocess.run([sys.executable, str(SCANNER_PATH), "--help"], capture_output=True, text=True)
    self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
  unittest.main()
