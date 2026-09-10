# SGP.22 v2.3: https://www.gsma.com/solutions-and-impact/technologies/esim/wp-content/uploads/2021/07/SGP.22-v2.3.pdf

import atexit
import base64
import fcntl
import hashlib
import os
import requests
import serial
import subprocess
import sys
import termios
import time

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from pathlib import Path

from openpilot.common.time_helpers import system_time_valid
from openpilot.system.hardware.base import LPABase, LPAError, LPAProfileNotFoundError, Profile

GSMA_CI_BUNDLE = str(Path(__file__).parent / "gsma_ci_bundle.pem")

DEFAULT_DEVICE = "/dev/modem_at0"
DEFAULT_BAUD = 9600
DEFAULT_TIMEOUT = 5.0
# https://euicc-manual.osmocom.org/docs/lpa/applet-id/
ISDR_AID = "A0000005591010FFFFFFFF8900000100"
ES10X_MSS = 120
HTTP_TIMEOUT = 30
OPEN_ISDR_RETRIES = 10
OPEN_ISDR_RETRY_DELAY_S = 0.25
SEND_APDU_RETRIES = 3
LOCK_FILE = '/dev/shm/modem.lock'
LOCK_TIMEOUT = 10.0
OPERATION_TIMEOUT = 45.0
MODEM_RESET_TIMEOUT = 20.0
LOCK_RETRY_DELAY_S = 0.05
CHANNEL_CLOSE_GRACE = 2.0
DEBUG = os.environ.get("DEBUG") == "1"


# TLV Tags
TAG_ICCID = 0x5A
TAG_STATUS = 0x80
TAG_EUICC_INFO = 0xBF20
TAG_PREPARE_DOWNLOAD = 0xBF21
TAG_BPP_COMMAND = 0xBF23
TAG_PROFILE_METADATA = 0xBF25
TAG_INSTALL_RESULT_DATA = 0xBF27
TAG_LIST_NOTIFICATION = 0xBF28
TAG_SET_NICKNAME = 0xBF29
TAG_RETRIEVE_NOTIFICATION = 0xBF2B
TAG_PROFILE_INFO_LIST = 0xBF2D
TAG_EUICC_CHALLENGE = 0xBF2E
TAG_NOTIFICATION_METADATA = 0xBF2F
TAG_NOTIFICATION_SENT = 0xBF30
TAG_ENABLE_PROFILE = 0xBF31
TAG_DELETE_PROFILE = 0xBF33
TAG_BPP = 0xBF36
TAG_PROFILE_INSTALL_RESULT = 0xBF37
TAG_AUTH_SERVER = 0xBF38
TAG_CANCEL_SESSION = 0xBF41
TAG_OK = 0xA0

PROFILE_OK = 0x00
PROFILE_NOT_IN_DISABLED_STATE = 0x02
PROFILE_CAT_BUSY = 0x05

PROFILE_ERROR_CODES = {
  0x01: "iccidOrAidNotFound", PROFILE_NOT_IN_DISABLED_STATE: "profileNotInDisabledState",
  0x03: "disallowedByPolicy", 0x04: "wrongProfileReenabling",
  PROFILE_CAT_BUSY: "catBusy", 0x06: "undefinedError",
}
AUTH_SERVER_ERROR_CODES = {
  0x01: "eUICCVerificationFailed", 0x02: "eUICCCertificateExpired",
  0x03: "eUICCCertificateRevoked", 0x05: "invalidServerSignature",
  0x06: "euiccCiPKUnknown", 0x0A: "matchingIdRefused",
  0x10: "insufficientMemory",
}
BPP_COMMAND_NAMES = {
  0: "initialiseSecureChannel", 1: "configureISDP", 2: "storeMetadata",
  3: "storeMetadata2", 4: "replaceSessionKeys", 5: "loadProfileElements",
}
BPP_ERROR_REASONS = {
  1: "incorrectInputValues", 2: "invalidSignature", 3: "invalidTransactionId",
  4: "unsupportedCrtValues", 5: "unsupportedRemoteOperationType",
  6: "unsupportedProfileClass", 7: "scp03tStructureError", 8: "scp03tSecurityError",
  9: "iccidAlreadyExistsOnEuicc", 10: "insufficientMemoryForProfile",
  11: "installInterrupted", 12: "peProcessingError", 13: "dataMismatch",
  14: "invalidNAA",
}
BPP_ERROR_MESSAGES = {
  9: "This eSIM profile is already installed on this device.",
  10: "Not enough memory on the eUICC to install this profile.",
  12: "Profile installation failed. The QR code may have already been used.",
}

# SGP.22 §5.2.6 SM-DP+ reason/subject codes mapped to user-friendly messages
ES9P_ERROR_MESSAGES: dict[tuple[str, str], str] = {
  ('3.8', '8.2.6'): "This eSIM profile is already installed on another device. Please use a new QR code.",
  ('3.8', '8.2.1'): "This eSIM profile has expired. Please request a new QR code.",
  ('3.8', '8.1'): "The SM-DP+ server refused this request.",
  ('3.1', '8.2.6'): "This eSIM profile has been revoked by the carrier.",
  ('3.9', '8.2.6'): "This eSIM profile download has already been completed.",
  ('2.1', '8.8'): "The device is not compatible with this eSIM profile.",
  ('1.2', '8.1'): "The SM-DP+ server is temporarily unavailable. Try again later.",
}

NOTIFICATION_OPERATIONS = {0x80: "install", 0x40: "enable", 0x20: "disable", 0x10: "delete"}

STATE_LABELS = {0: "disabled", 1: "enabled", 255: "unknown"}
ICON_LABELS = {0: "jpeg", 1: "png", 255: "unknown"}
CLASS_LABELS = {0: "test", 1: "provisioning", 2: "operational", 255: "unknown"}

# TLV tag -> (field_name, decoder)
FieldMap = dict[int, tuple[str, Callable[[bytes], Any]]]


class LPABusy(LPAError):
  pass


class LPAMutationAmbiguous(LPAError):
  pass


class LPADeadlineExceeded(LPAError):
  pass


class LPAProfileStateError(LPAError):
  pass


class LPAProvisioningUnknown(LPAError):
  pass


class LPANotificationDeliveryError(LPAError):
  pass


@dataclass(frozen=True)
class Notification:
  sequence: int
  operation: str
  iccid: str


@dataclass(frozen=True)
class RetrievedNotification:
  sequence: int
  operation: str
  iccid: str
  encoded_tlv: bytes = b""
  notification_address: str = field(default="", repr=False)


class OperationDeadline:
  def __init__(self, timeout: float = OPERATION_TIMEOUT, now: Callable[[], float] = time.monotonic) -> None:
    if timeout <= 0:
      raise ValueError("operation timeout must be positive")
    self._now = now
    self.expires_at = now() + timeout

  def remaining(self) -> float:
    remaining = self.expires_at - self._now()
    if remaining <= 0:
      raise LPADeadlineExceeded("eSIM operation deadline exceeded")
    return remaining


def b64e(data: bytes) -> str:
  return base64.b64encode(data).decode("ascii")


def base64_trim(s: str) -> str:
  return "".join(c for c in s if c not in "\n\r \t")


def b64d(s: str) -> bytes:
  return base64.b64decode(base64_trim(s))


class AtClient:
  def __init__(self, device: str, baud: int, timeout: float) -> None:
    self.channel: str | None = None
    self._device = device
    self._baud = baud
    self._timeout = timeout
    self._serial: serial.Serial | None = None

  def send_raw(self, data: bytes) -> None:
    self._ensure_serial()
    self._serial.reset_input_buffer()
    self._serial.write(data)
    self._serial.flush()

  def close(self) -> None:
    try:
      if self.channel:
        try:
          self.query(f"AT+CCHC={self.channel}")
        except (RuntimeError, TimeoutError):
          pass
        self.channel = None
    finally:
      if self._serial:
        self._serial.close()

  def _send(self, cmd: str) -> None:
    if DEBUG:
      print(f"SER >> {cmd}", file=sys.stderr)
    self._serial.write((cmd + "\r").encode("ascii"))

  def _expect(self, deadline: OperationDeadline | None = None) -> list[str]:
    lines: list[str] = []
    while True:
      if deadline is not None:
        self._serial.timeout = min(self._timeout, deadline.remaining())
      raw = self._serial.readline()
      if not raw:
        raise TimeoutError("AT command timed out")
      line = raw.decode(errors="ignore").strip()
      if not line:
        continue
      if DEBUG:
        print(f"SER << {line}", file=sys.stderr)
      if line == "OK":
        return lines
      if line == "ERROR" or line.startswith("+CME ERROR"):
        raise RuntimeError(f"AT command failed: {line}")
      lines.append(line)

  def _ensure_serial(self, reconnect: bool = False, deadline: OperationDeadline | None = None) -> None:
    if reconnect:
      self.channel = None
      try:
        if self._serial:
          self._serial.close()
      except Exception:
        pass
      self._serial = None
    if self._serial is None:
      timeout = self._timeout if deadline is None else min(self._timeout, deadline.remaining())
      self._serial = serial.Serial(self._device, baudrate=self._baud, timeout=timeout)

  def query(self, cmd: str, *, deadline: OperationDeadline | None = None, retry: bool = True) -> list[str]:
    self._ensure_serial(deadline=deadline)
    try:
      self._send(cmd)
      return self._expect(deadline)
    except serial.SerialException:
      if not retry:
        raise
      self._ensure_serial(reconnect=True, deadline=deadline)
      self._send(cmd)
      return self._expect(deadline)

  def _open_isdr_once(self, deadline: OperationDeadline | None = None) -> None:
    if self.channel:
      try:
        self.query(f"AT+CCHC={self.channel}", deadline=deadline)
      except RuntimeError:
        pass
      self.channel = None
    # drain any unsolicited responses before opening
    if self._serial:
      try:
        self._serial.reset_input_buffer()
      except (OSError, serial.SerialException, termios.error):
        self._ensure_serial(reconnect=True, deadline=deadline)
    for line in self.query(f'AT+CCHO="{ISDR_AID}"', deadline=deadline):
      if line.startswith("+CCHO:") and (ch := line.split(":", 1)[1].strip()):
        self.channel = ch
        return
    raise RuntimeError("Failed to open ISD-R application")

  def reset_modem(self, deadline: OperationDeadline) -> None:
    if self._serial:
      try:
        self._serial.close()
      except Exception:
        pass
      self._serial = None
    self.channel = None
    timeout = min(MODEM_RESET_TIMEOUT, deadline.remaining())
    try:
      result = subprocess.run(['/usr/comma/lte/lte.sh', 'start'], capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
      raise LPADeadlineExceeded("modem reset/start timed out before profile mutation") from None
    if result.returncode != 0:
      raise LPAError(f"modem reset/start failed before profile mutation (exit {result.returncode})")

  def open_isdr(self, deadline: OperationDeadline | None = None) -> None:
    for attempt in range(OPEN_ISDR_RETRIES):
      try:
        self._open_isdr_once(deadline)
        return
      except (RuntimeError, TimeoutError, termios.error, serial.SerialException):
        if attempt == OPEN_ISDR_RETRIES - 1:
          break
        delay = OPEN_ISDR_RETRY_DELAY_S
        if deadline is not None:
          delay = min(delay, deadline.remaining())
        time.sleep(delay)
    raise RuntimeError("Failed to open ISD-R after retries")

  def send_apdu(self, apdu: bytes, *, deadline: OperationDeadline | None = None,
                allow_retry: bool = True) -> tuple[bytes, int, int]:
    attempts = SEND_APDU_RETRIES if allow_retry else 1
    for attempt in range(attempts):
      try:
        if not self.channel:
          self.open_isdr(deadline)
        hex_payload = apdu.hex().upper()
        for line in self.query(f'AT+CGLA={self.channel},{len(hex_payload)},"{hex_payload}"',
                               deadline=deadline, retry=allow_retry):
          if line.startswith("+CGLA:"):
            parts = line.split(":", 1)[1].split(",", 1)
            if len(parts) == 2:
              data = bytes.fromhex(parts[1].strip().strip('"'))
              if len(data) >= 2:
                if data[-2:] == b'\x68\x81' and allow_retry and attempt < attempts - 1:
                  self.channel = None
                  break
                return data[:-2], data[-2], data[-1]
        else:
          raise RuntimeError("Missing +CGLA response")
      except (RuntimeError, ValueError, TimeoutError, serial.SerialException) as error:
        self.channel = None
        if not allow_retry:
          raise LPAMutationAmbiguous("mutating APDU result is unknown; command was not retried") from None
        if attempt == attempts - 1:
          raise
      if deadline is not None:
        deadline.remaining()
    raise RuntimeError("send_apdu failed")


# --- TLV utilities ---

def iter_tlv(data: bytes, with_positions: bool = False) -> Generator:
  idx, length = 0, len(data)
  while idx < length:
    start_pos = idx
    tag = data[idx]
    idx += 1
    if tag & 0x1F == 0x1F:  # Multi-byte tag
      tag_value = tag
      while idx < length:
        next_byte = data[idx]
        idx += 1
        tag_value = (tag_value << 8) | next_byte
        if not (next_byte & 0x80):
          break
    else:
      tag_value = tag
    if idx >= length:
      break
    size = data[idx]
    idx += 1
    if size & 0x80:  # Multi-byte length
      num_bytes = size & 0x7F
      if idx + num_bytes > length:
        break
      size = int.from_bytes(data[idx : idx + num_bytes], "big")
      idx += num_bytes
    if idx + size > length:
      break
    value = data[idx : idx + size]
    idx += size
    yield (tag_value, value, start_pos, idx) if with_positions else (tag_value, value)


def find_tag(data: bytes, target: int) -> bytes | None:
  return next((v for t, v in iter_tlv(data) if t == target), None)


def require_tag(data: bytes, target: int, label: str = "") -> bytes:
  v = find_tag(data, target)
  if v is None:
    raise RuntimeError(f"Missing {label or f'tag 0x{target:X}'}")
  return v


def tbcd_to_string(raw: bytes) -> str:
  return "".join(str(n) for b in raw for n in (b & 0x0F, b >> 4) if n <= 9)


def string_to_tbcd(s: str) -> bytes:
  digits = [int(c) for c in s if c.isdigit()]
  return bytes(digits[i] | ((digits[i + 1] if i + 1 < len(digits) else 0xF) << 4) for i in range(0, len(digits), 2))


def encode_tlv(tag: int, value: bytes) -> bytes:
  tag_bytes = bytes([(tag >> 8) & 0xFF, tag & 0xFF]) if tag > 255 else bytes([tag])
  vlen = len(value)
  if vlen <= 127:
    return tag_bytes + bytes([vlen]) + value
  length_bytes = vlen.to_bytes((vlen.bit_length() + 7) // 8, "big")
  return tag_bytes + bytes([0x80 | len(length_bytes)]) + length_bytes + value


def int_bytes(n: int) -> bytes:
  """Encode a positive integer as minimal big-endian bytes (at least 1 byte)."""
  return n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")


PROFILE: FieldMap = {
  TAG_ICCID: ("iccid", tbcd_to_string),
  0x4F: ("isdpAid", lambda v: v.hex().upper()),
  0x9F70: ("profileState", lambda v: STATE_LABELS.get(v[0], "unknown")),
  0x90: ("profileNickname", lambda v: v.decode("utf-8", errors="ignore") or None),
  0x91: ("serviceProviderName", lambda v: v.decode("utf-8", errors="ignore") or None),
  0x92: ("profileName", lambda v: v.decode("utf-8", errors="ignore") or None),
  0x93: ("iconType", lambda v: ICON_LABELS.get(v[0], "unknown")),
  0x94: ("icon", b64e),
  0x95: ("profileClass", lambda v: CLASS_LABELS.get(v[0], "unknown")),
}


def decode_struct(data: bytes, field_map: FieldMap) -> dict[str, Any]:
  """Parse TLV data using a {tag: (field_name, decoder)} map into a dict."""
  result: dict[str, Any] = {name: None for name, _ in field_map.values()}
  for tag, value in iter_tlv(data):
    if (field := field_map.get(tag)):
      result[field[0]] = field[1](value)
  return result


# --- ES10x command transport ---

def es10x_command(client: AtClient, data: bytes, *, mutating: bool = False,
                  deadline: OperationDeadline | None = None) -> bytes:
  response = bytearray()
  sequence = 0
  offset = 0
  while offset < len(data):
    chunk = data[offset : offset + ES10X_MSS]
    offset += len(chunk)
    is_last = offset == len(data)
    apdu = bytes([0x80, 0xE2, 0x91 if is_last else 0x11, sequence & 0xFF, len(chunk)]) + chunk
    segment, sw1, sw2 = client.send_apdu(apdu, deadline=deadline, allow_retry=not mutating)
    response.extend(segment)
    while True:
      if sw1 == 0x61:  # More data available
        segment, sw1, sw2 = client.send_apdu(bytes([0x80, 0xC0, 0x00, 0x00, sw2 or 0]),
                                             deadline=deadline, allow_retry=not mutating)
        response.extend(segment)
        continue
      if (sw1 & 0xF0) == 0x90:
        break
      if mutating:
        raise LPAMutationAmbiguous(f"mutating APDU stopped at SW={sw1:02X}{sw2:02X}; command was not retried")
      raise RuntimeError(f"APDU failed with SW={sw1:02X}{sw2:02X}")
    sequence += 1
  return bytes(response)


# --- Profile operations ---

NOTIFICATION: FieldMap = {
  TAG_STATUS: ("seqNumber", lambda v: int.from_bytes(v, "big")),
  0x81: ("profileManagementOperation",
         lambda v: NOTIFICATION_OPERATIONS.get(next((m for m in NOTIFICATION_OPERATIONS if len(v) >= 2 and v[1] & m), 0), "unknown")),
  0x0C: ("notificationAddress", lambda v: v.decode("utf-8", errors="ignore")),
  TAG_ICCID: ("iccid", tbcd_to_string),
}


def decode_profiles(blob: bytes) -> list[dict]:
  root = require_tag(blob, TAG_PROFILE_INFO_LIST, "ProfileInfoList")
  list_ok = find_tag(root, TAG_OK)
  if list_ok is None:
    return []
  return [decode_struct(value, PROFILE) for tag, value in iter_tlv(list_ok) if tag == 0xE3]


def validate_profiles(records: list[dict]) -> list[Profile]:
  profiles: list[Profile] = []
  seen: set[str] = set()
  for record in records:
    iccid = record.get("iccid")
    state = record.get("profileState")
    if not isinstance(iccid, str) or not (18 <= len(iccid) <= 22) or not iccid.isdecimal():
      raise LPAProfileStateError("eUICC returned a profile with an invalid ICCID")
    if iccid in seen:
      raise LPAProfileStateError("eUICC returned duplicate profile ICCIDs")
    if state not in ("enabled", "disabled"):
      raise LPAProfileStateError(f"profile ***{iccid[-4:]} has an unknown state")
    seen.add(iccid)
    profiles.append(Profile(
      iccid=iccid,
      nickname=record.get("profileNickname") or "",
      enabled=state == "enabled",
      provider=record.get("serviceProviderName") or "",
    ))
  return profiles


def require_one_active_profile(profiles: list[Profile]) -> Profile:
  active = [profile for profile in profiles if profile.enabled]
  if len(active) != 1:
    raise LPAProfileStateError(f"expected exactly one enabled eUICC profile; observed {len(active)}")
  return active[0]


def is_protected_profile(profile: Profile) -> bool:
  provider = profile.provider.casefold()
  nickname = profile.nickname.casefold()
  return (profile.is_comma or provider == "webbing" or profile.iccid.startswith("8985235") or
          "betterroaming" in provider or nickname == "test-esim" or
          "telekom" in provider or "telekom" in nickname)


def list_profiles(client: AtClient, deadline: OperationDeadline | None = None) -> list[dict]:
  return decode_profiles(es10x_command(client, TAG_PROFILE_INFO_LIST.to_bytes(2, "big") + b"\x00", deadline=deadline))


def set_profile_nickname(client: AtClient, iccid: str, nickname: str,
                         deadline: OperationDeadline | None = None) -> None:
  nickname_bytes = nickname.encode("utf-8")
  if len(nickname_bytes) > 64:
    raise ValueError("Profile nickname must be 64 bytes or less")
  content = encode_tlv(TAG_ICCID, string_to_tbcd(iccid)) + encode_tlv(0x90, nickname_bytes)
  response = es10x_command(client, encode_tlv(TAG_SET_NICKNAME, content), mutating=True, deadline=deadline)
  code = require_tag(require_tag(response, TAG_SET_NICKNAME, "SetNicknameResponse"), TAG_STATUS, "SetNickname status")[0]
  if code == 0x01:
    raise LPAError(f"profile {iccid} not found")
  if code != 0x00:
    raise RuntimeError(f"SetNickname failed with status 0x{code:02X}")


# --- ES9P HTTP ---

def es9p_request(smdp_address: str, endpoint: str, payload: dict, error_prefix: str = "Request",
                 session: requests.Session | None = None, deadline: OperationDeadline | None = None) -> dict:
  url = f"https://{smdp_address}/gsma/rsp2/es9plus/{endpoint}"
  headers = {"User-Agent": "gsma-rsp-lpad", "X-Admin-Protocol": "gsma/rsp/v2.3.0", "Content-Type": "application/json"}
  http = session or requests
  timeout = HTTP_TIMEOUT if deadline is None else min(HTTP_TIMEOUT, deadline.remaining())
  resp = http.post(url, json=payload, headers=headers, timeout=timeout, verify=GSMA_CI_BUNDLE)
  resp.raise_for_status()
  if not resp.content:
    return {}
  data = resp.json()
  if "header" in data and "functionExecutionStatus" in data["header"]:
    status = data["header"]["functionExecutionStatus"]
    if status.get("status") == "Failed":
      sd = status.get("statusCodeData", {})
      reason = sd.get("reasonCode", "unknown")
      subject = sd.get("subjectCode", "unknown")
      msg = ES9P_ERROR_MESSAGES.get((reason, subject),
            f"{error_prefix} failed: {reason}/{subject} - {sd.get('message', 'unknown')}")
      raise RuntimeError(msg)
  return data


# --- Notifications ---

def list_notifications(client: AtClient, deadline: OperationDeadline | None = None) -> list[dict]:
  response = es10x_command(client, encode_tlv(TAG_LIST_NOTIFICATION, b""), deadline=deadline)
  root = require_tag(response, TAG_LIST_NOTIFICATION, "ListNotificationResponse")
  metadata_list = find_tag(root, TAG_OK)
  if metadata_list is None:
    return []
  return [decode_struct(value, NOTIFICATION) for tag, value in iter_tlv(metadata_list) if tag == TAG_NOTIFICATION_METADATA]


def retrieve_notification(client: AtClient, notification: dict,
                          deadline: OperationDeadline | None = None) -> RetrievedNotification:
  sequence = notification["seqNumber"]
  request = encode_tlv(TAG_RETRIEVE_NOTIFICATION, encode_tlv(TAG_OK, encode_tlv(TAG_STATUS, int_bytes(sequence))))
  response = es10x_command(client, request, deadline=deadline)
  content = require_tag(require_tag(response, TAG_RETRIEVE_NOTIFICATION, "RetrieveNotificationsListResponse"),
                        TAG_OK, "RetrieveNotificationsListResponse")
  # Keep the complete encoded TLV, including tag and length, as fixed in upstream openpilot.
  pending = next((content[start:end] for tag, _, start, end in iter_tlv(content, with_positions=True)
                  if tag in (TAG_PROFILE_INSTALL_RESULT, 0x30)), None)
  if pending is None:
    raise RuntimeError("Missing PendingNotification")
  return RetrievedNotification(
    sequence=sequence,
    operation=notification.get("profileManagementOperation") or "unknown",
    iccid=notification.get("iccid") or "",
    encoded_tlv=pending,
    notification_address=notification.get("notificationAddress") or "",
  )


def deliver_notification_once(notification: RetrievedNotification,
                              deadline: OperationDeadline | None = None) -> None:
  if not notification.notification_address:
    raise LPANotificationDeliveryError("selected notification has no delivery endpoint")
  try:
    es9p_request(notification.notification_address, "handleNotification",
                 {"pendingNotification": b64e(notification.encoded_tlv)}, "HandleNotification", deadline=deadline)
  except Exception:
    raise LPANotificationDeliveryError(
      "notification delivery was not accepted or its result is unknown; it was not retried") from None


def remove_notification(client: AtClient, sequence: int,
                        deadline: OperationDeadline | None = None) -> None:
  response = es10x_command(client, encode_tlv(TAG_NOTIFICATION_SENT, encode_tlv(TAG_STATUS, int_bytes(sequence))),
                           mutating=True, deadline=deadline)
  root = require_tag(response, TAG_NOTIFICATION_SENT, "NotificationSentResponse")
  if int.from_bytes(require_tag(root, TAG_STATUS, "RemoveNotificationFromList status"), "big") != 0:
    raise LPAMutationAmbiguous("notification was delivered but queue removal did not succeed")


# --- Authentication & Download ---

def get_challenge_and_info(client: AtClient, deadline: OperationDeadline | None = None) -> tuple[bytes, bytes]:
  challenge_resp = es10x_command(client, encode_tlv(TAG_EUICC_CHALLENGE, b""), deadline=deadline)
  challenge = require_tag(require_tag(challenge_resp, TAG_EUICC_CHALLENGE, "GetEuiccDataResponse"),
                          TAG_STATUS, "challenge in response")
  info_resp = es10x_command(client, encode_tlv(TAG_EUICC_INFO, b""), deadline=deadline)
  require_tag(info_resp, TAG_EUICC_INFO, "GetEuiccInfo1Response")
  return challenge, info_resp


def authenticate_server(client: AtClient, b64_signed1: str, b64_sig1: str, b64_pk_id: str,
                        b64_cert: str, matching_id: str, deadline: OperationDeadline | None = None) -> str:
  tac = bytes([0x35, 0x29, 0x06, 0x11])
  device_info = encode_tlv(TAG_STATUS, tac) + encode_tlv(0xA1, b"")
  ctx_inner = encode_tlv(TAG_STATUS, matching_id.encode("utf-8")) + encode_tlv(0xA1, device_info)
  content = b64d(b64_signed1) + b64d(b64_sig1) + b64d(b64_pk_id) + b64d(b64_cert) + encode_tlv(0xA0, ctx_inner)
  response = es10x_command(client, encode_tlv(TAG_AUTH_SERVER, content), mutating=True, deadline=deadline)
  root = require_tag(response, TAG_AUTH_SERVER, "AuthenticateServerResponse")
  error_tag = find_tag(root, 0xA1)
  if error_tag is not None:
    code = int.from_bytes(error_tag, "big") if error_tag else 0
    raise RuntimeError(f"AuthenticateServer rejected by eUICC: {AUTH_SERVER_ERROR_CODES.get(code, 'unknown')} (0x{code:02X})")
  return b64e(response)


def prepare_download(client: AtClient, b64_signed2: str, b64_sig2: str, b64_cert: str,
                     cc: str | None = None, deadline: OperationDeadline | None = None) -> str:
  smdp_signed2 = b64d(b64_signed2)
  smdp_signature2 = b64d(b64_sig2)
  smdp_certificate = b64d(b64_cert)
  smdp_signed2_root = find_tag(smdp_signed2, 0x30)
  if smdp_signed2_root is None:
    raise RuntimeError("Invalid smdpSigned2")
  transaction_id = find_tag(smdp_signed2_root, TAG_STATUS)
  cc_required_flag = find_tag(smdp_signed2_root, 0x01)
  if transaction_id is None or cc_required_flag is None:
    raise RuntimeError("Invalid smdpSigned2")
  content = smdp_signed2 + smdp_signature2
  if int.from_bytes(cc_required_flag, "big") != 0:
    if not cc:
      raise RuntimeError("Confirmation code required but not provided")
    content += encode_tlv(0x04, hashlib.sha256(hashlib.sha256(cc.encode("utf-8")).digest() + transaction_id).digest())
  content += smdp_certificate
  response = es10x_command(client, encode_tlv(TAG_PREPARE_DOWNLOAD, content), mutating=True, deadline=deadline)
  require_tag(response, TAG_PREPARE_DOWNLOAD, "PrepareDownloadResponse")
  return b64e(response)


def _parse_tlv_header_len(data: bytes) -> int:
  tag_len = 2 if data[0] & 0x1F == 0x1F else 1
  length_byte = data[tag_len]
  return tag_len + (1 + (length_byte & 0x7F) if length_byte & 0x80 else 1)


def _split_bpp(bpp: bytes) -> list[bytes]:
  """Split a BoundProfilePackage into APDU chunks per SGP.22 §5.7.6."""
  root_value = None
  for tag, value, start, end in iter_tlv(bpp, with_positions=True):
    if tag == TAG_BPP:
      root_value = value
      val_start = start + _parse_tlv_header_len(bpp[start:end])
      break
  if root_value is None:
    raise RuntimeError("Invalid BoundProfilePackage")

  chunks: list[bytes] = []
  for tag, value, start, end in iter_tlv(root_value, with_positions=True):
    if tag == TAG_BPP_COMMAND:
      chunks.append(bpp[0 : val_start + end])
    elif tag in (0xA0, 0xA2):
      chunks.append(bpp[val_start + start : val_start + end])
    elif tag in (0xA1, 0xA3):
      hdr_len = _parse_tlv_header_len(root_value[start:end])
      chunks.append(bpp[val_start + start : val_start + start + hdr_len])
      for _, _, cs, ce in iter_tlv(value, with_positions=True):
        chunks.append(value[cs:ce])
  return chunks


def _parse_install_result(response: bytes) -> dict[str, Any] | None:
  """Parse a ProfileInstallResult from an APDU response, or None if not present."""
  root = find_tag(response, TAG_PROFILE_INSTALL_RESULT)
  if not root:
    return None
  result_data = find_tag(root, TAG_INSTALL_RESULT_DATA)
  if not result_data:
    return None
  result: dict[str, Any] = {"seqNumber": 0, "success": False, "bppCommandId": None, "errorReason": None}
  notif_meta = find_tag(result_data, TAG_NOTIFICATION_METADATA)
  if notif_meta:
    seq_num = find_tag(notif_meta, TAG_STATUS)
    if seq_num:
      result["seqNumber"] = int.from_bytes(seq_num, "big")
  final_result = find_tag(result_data, 0xA2)
  if final_result:
    for tag, value in iter_tlv(final_result):
      if tag == 0xA0:
        result["success"] = True
      elif tag == 0xA1:
        bpp_cmd = find_tag(value, TAG_STATUS)
        if bpp_cmd:
          result["bppCommandId"] = int.from_bytes(bpp_cmd, "big")
        err = find_tag(value, 0x81)
        if err:
          result["errorReason"] = int.from_bytes(err, "big")
  return result


def load_bpp(client: AtClient, b64_bpp: str, deadline: OperationDeadline | None = None) -> dict:
  bpp = b64d(b64_bpp)
  result = None
  for chunk in _split_bpp(bpp):
    response = es10x_command(client, chunk, mutating=True, deadline=deadline)
    if response and (parsed := _parse_install_result(response)):
      result = parsed
      break

  if result is None:
    raise RuntimeError("Profile installation failed: no result from eUICC")
  if not result["success"] and result["errorReason"] is not None:
    msg = BPP_ERROR_MESSAGES.get(result["errorReason"])
    if not msg:
      cmd_name = BPP_COMMAND_NAMES.get(result["bppCommandId"], f"unknown({result['bppCommandId']})")
      err_name = BPP_ERROR_REASONS.get(result["errorReason"], f"unknown({result['errorReason']})")
      msg = f"Profile installation failed at {cmd_name}: {err_name}"
    raise RuntimeError(msg)
  if not result["success"]:
    raise RuntimeError("Profile installation failed: no result from eUICC")
  return result


def parse_metadata(b64_metadata: str) -> dict:
  root = find_tag(b64d(b64_metadata), TAG_PROFILE_METADATA)
  if root is None:
    raise RuntimeError("Invalid profileMetadata")
  return decode_struct(root, PROFILE)


def cancel_session(client: AtClient, transaction_id: bytes, reason: int = 127,
                   deadline: OperationDeadline | None = None) -> str:
  content = encode_tlv(0x80, transaction_id) + encode_tlv(0x81, bytes([reason]))
  response = es10x_command(client, encode_tlv(TAG_CANCEL_SESSION, content), mutating=True, deadline=deadline)
  return b64e(response)


def parse_lpa_activation_code(activation_code: str) -> tuple[str, str]:
  """Parse 'LPA:1$smdp.example.com$MATCHING-ID' into (smdp_address, matching_id)."""
  if not activation_code.startswith("LPA:"):
    raise ValueError("Invalid activation code format")
  parts = activation_code[4:].split("$")
  if len(parts) != 3 or parts[0] != "1" or not parts[1] or not parts[2]:
    raise ValueError("Invalid activation code format")
  return parts[1], parts[2]


def _b64_field(data: dict, key: str) -> str:
  return base64_trim(data[key])


def _cancel_session_safe(client: AtClient, smdp: str, tx_id: str, session: requests.Session,
                         deadline: OperationDeadline | None = None) -> None:
  b64_cancel = ""
  try:
    b64_cancel = cancel_session(client, b64d(tx_id), deadline=deadline)
  except Exception:
    pass
  try:
    es9p_request(smdp, "cancelSession", {"transactionId": tx_id, "cancelSessionResponse": b64_cancel},
                 "CancelSession", session=session, deadline=deadline)
  except Exception:
    pass


def download_profile(client: AtClient, activation_code: str, deadline: OperationDeadline | None = None,
                     on_submission: Callable[[], None] | None = None) -> str:
  """Download and install an eSIM profile. Returns the ICCID of the installed profile."""
  if not system_time_valid():
    raise RuntimeError("System time is not set; TLS certificate validation requires a valid clock")
  smdp, matching_id = parse_lpa_activation_code(activation_code)
  challenge, euicc_info = get_challenge_and_info(client, deadline)
  session = requests.Session()
  tx_id = None
  submitted = False

  try:
    # step 1: initiate authentication
    if on_submission is not None:
      on_submission()
    submitted = True
    auth = es9p_request(smdp, "initiateAuthentication", {
      "smdpAddress": smdp, "euiccChallenge": b64e(challenge),
      "euiccInfo1": b64e(euicc_info), "matchingId": matching_id,
    }, "Authentication", session=session, deadline=deadline)
    tx_id = _b64_field(auth, "transactionId")

    # step 2: authenticate server
    b64_auth = authenticate_server(client,
      _b64_field(auth, "serverSigned1"), _b64_field(auth, "serverSignature1"),
      _b64_field(auth, "euiccCiPKIdToBeUsed"), _b64_field(auth, "serverCertificate"),
      matching_id, deadline)

    # step 3: authenticate client + get metadata
    cli = es9p_request(smdp, "authenticateClient", {
      "transactionId": tx_id, "authenticateServerResponse": b64_auth,
    }, "Authentication", session=session, deadline=deadline)
    iccid = parse_metadata(_b64_field(cli, "profileMetadata"))["iccid"]

    # step 4: prepare download
    b64_prep = prepare_download(client,
      _b64_field(cli, "smdpSigned2"), _b64_field(cli, "smdpSignature2"),
      _b64_field(cli, "smdpCertificate"), deadline=deadline)

    # step 5: get and install bound profile package
    bpp = es9p_request(smdp, "getBoundProfilePackage", {
      "transactionId": tx_id, "prepareDownloadResponse": b64_prep,
    }, "GetBoundProfilePackage", session=session, deadline=deadline)
    load_bpp(client, _b64_field(bpp, "boundProfilePackage"), deadline)
    return iccid
  except Exception:
    if tx_id:
      _cancel_session_safe(client, smdp, tx_id, session, deadline)
    if submitted:
      raise LPAProvisioningUnknown(
        "profile download result is unknown; activation code was not resubmitted and read-only reconciliation is required") from None
    raise
  finally:
    session.close()


def _typed_param_bool(params: Any, key: str) -> bool:
  value = params.get(key)
  if isinstance(value, bytes):
    value = value.decode("ascii", errors="strict")
  if value in (True, "1"):
    return True
  if value in (False, "0"):
    return False
  raise LPAError(f"{key} is missing or is not a typed boolean")


def require_explicit_offroad(params: Any | None = None) -> None:
  if params is None:
    from openpilot.common.params import Params
    params = Params()
  if _typed_param_bool(params, "IsOnroad") or not _typed_param_bool(params, "IsOffroad"):
    raise LPAError("eSIM mutation requires IsOnroad=false and IsOffroad=true")


class TiciLPA(LPABase):
  def __init__(self, client: AtClient | None = None, *, lock_file: str = LOCK_FILE,
               params: Any | None = None, now: Callable[[], float] = time.monotonic,
               sleep: Callable[[float], None] = time.sleep):
    self._client = client or AtClient(DEFAULT_DEVICE, DEFAULT_BAUD, DEFAULT_TIMEOUT)
    self._lock_file = lock_file
    self._params = params
    self._now = now
    self._sleep = sleep
    atexit.register(self._client.close)

  def _deadline(self, timeout: float = OPERATION_TIMEOUT) -> OperationDeadline:
    return OperationDeadline(timeout, self._now)

  @contextmanager
  def _acquire_lock(self, deadline: OperationDeadline):
    fd = os.open(self._lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    lock_expires_at = min(deadline.expires_at, self._now() + LOCK_TIMEOUT)
    try:
      while True:
        try:
          fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
          locked = True
          break
        except BlockingIOError:
          if self._now() >= lock_expires_at:
            raise LPADeadlineExceeded("timed out acquiring shared modem lock")
          delay = min(LOCK_RETRY_DELAY_S, deadline.remaining())
          self._sleep(delay)
      yield
    finally:
      if locked:
        fcntl.flock(fd, fcntl.LOCK_UN)
      os.close(fd)

  def _close_channel(self, deadline: OperationDeadline) -> None:
    channel = self._client.channel
    self._client.channel = None
    if channel:
      try:
        # Cleanup has its own short grace so an expired operation deadline cannot skip CCHC.
        cleanup_deadline = OperationDeadline(CHANNEL_CLOSE_GRACE, self._now)
        self._client.query(f"AT+CCHC={channel}", deadline=cleanup_deadline)
      except Exception:
        pass

  @contextmanager
  def _acquire_channel(self, deadline: OperationDeadline):
    with self._acquire_lock(deadline):
      try:
        self._client.open_isdr(deadline)
        yield
      finally:
        self._close_channel(deadline)

  @contextmanager
  def _prepared_mutation_channel(self, deadline: OperationDeadline):
    with self._acquire_lock(deadline):
      try:
        self._client.reset_modem(deadline)
        self._client.open_isdr(deadline)
        yield
      finally:
        self._close_channel(deadline)

  def _list_profiles(self, deadline: OperationDeadline) -> list[Profile]:
    with self._acquire_channel(deadline):
      return validate_profiles(list_profiles(self._client, deadline))

  def list_profiles(self) -> list[Profile]:
    return self._list_profiles(self._deadline())

  def get_profile_state(self) -> tuple[list[Profile], Profile]:
    profiles = self._list_profiles(self._deadline())
    return profiles, require_one_active_profile(profiles)

  def get_active_profile(self) -> Profile:
    return self.get_profile_state()[1]

  def process_notifications(self) -> None:
    raise LPAError("blanket notification processing is disabled; select one fresh notification explicitly")

  def _notification_records(self, deadline: OperationDeadline) -> list[dict]:
    with self._acquire_channel(deadline):
      records = list_notifications(self._client, deadline)
    sequences: set[int] = set()
    for record in records:
      sequence = record.get("seqNumber")
      if not isinstance(sequence, int) or sequence < 0 or sequence in sequences:
        raise LPAError("eUICC returned invalid or duplicate notification sequence numbers")
      sequences.add(sequence)
    return records

  def list_notifications(self) -> list[Notification]:
    return [Notification(record["seqNumber"], record.get("profileManagementOperation") or "unknown",
                         record.get("iccid") or "")
            for record in self._notification_records(self._deadline())]

  def process_selected_notification(self, sequence: int, operation: str, iccid: str,
                                    preexisting_sequences: set[int]) -> dict[str, Any]:
    require_explicit_offroad(self._params)
    deadline = self._deadline()
    records = self._notification_records(deadline)
    before_sequences = {record["seqNumber"] for record in records}
    candidates = [record for record in records
                  if record["seqNumber"] == sequence and record.get("profileManagementOperation") == operation
                  and record.get("iccid") == iccid and sequence not in preexisting_sequences]
    if len(candidates) != 1:
      raise LPAError(f"expected one fresh notification matching sequence/operation/ICCID; observed {len(candidates)}")
    with self._acquire_channel(deadline):
      selected = retrieve_notification(self._client, candidates[0], deadline)
    if not system_time_valid():
      raise LPANotificationDeliveryError("system time is not set; notification was retained")
    deliver_notification_once(selected, deadline)
    try:
      with self._acquire_channel(deadline):
        remove_notification(self._client, sequence, deadline)
    except Exception:
      raise LPAMutationAmbiguous(
        "notification delivery was accepted but removal result is unknown; delivery was not retried") from None
    after_sequences = {record["seqNumber"] for record in self._notification_records(deadline)}
    expected = before_sequences - {sequence}
    if after_sequences != expected:
      raise LPAMutationAmbiguous("selected notification removal verification found an unexpected queue change")
    return {"sequence": sequence, "operation": operation, "iccid": iccid,
            "delivery": "accepted", "queue": "selected_sequence_removed"}

  def delete_profile(self, iccid: str) -> None:
    require_explicit_offroad(self._params)
    deadline = self._deadline()
    profile = next((p for p in self._list_profiles(deadline) if p.iccid == iccid), None)
    if profile is None:
      raise LPAProfileNotFoundError(f"profile not found: {iccid}")
    if is_protected_profile(profile):
      raise LPAError(f"refusing to delete protected profile ***{iccid[-4:]}")
    with self._acquire_channel(deadline):
      request = encode_tlv(TAG_DELETE_PROFILE, encode_tlv(TAG_ICCID, string_to_tbcd(iccid)))
      response = es10x_command(self._client, request, mutating=True, deadline=deadline)
      code = require_tag(require_tag(response, TAG_DELETE_PROFILE, "DeleteProfileResponse"), TAG_STATUS, "DeleteProfile status")[0]
    if code != PROFILE_OK:
      raise LPAError(f"DeleteProfile failed: {PROFILE_ERROR_CODES.get(code, 'unknown')} (0x{code:02X})")

  def download_profile(self, qr: str, nickname: str | None = None,
                       on_submission: Callable[[], None] | None = None) -> str:
    require_explicit_offroad(self._params)
    deadline = self._deadline()
    with self._acquire_channel(deadline):
      iccid = download_profile(self._client, qr, deadline, on_submission)
      if nickname and iccid:
        set_profile_nickname(self._client, iccid, nickname, deadline)
    return iccid

  def nickname_profile(self, iccid: str, nickname: str) -> None:
    require_explicit_offroad(self._params)
    deadline = self._deadline()
    with self._acquire_channel(deadline):
      set_profile_nickname(self._client, iccid, nickname, deadline)

  def _enable_profile(self, iccid: str, deadline: OperationDeadline) -> int:
    inner = encode_tlv(TAG_OK, encode_tlv(TAG_ICCID, string_to_tbcd(iccid)))
    inner += b'\x01\x01\x01'  # refreshFlag=1
    response = es10x_command(self._client, encode_tlv(TAG_ENABLE_PROFILE, inner), mutating=True, deadline=deadline)
    return require_tag(require_tag(response, TAG_ENABLE_PROFILE, "EnableProfileResponse"), TAG_STATUS, "EnableProfile status")[0]

  def prepare_and_switch_profile(self, iccid: str) -> Profile:
    require_explicit_offroad(self._params)
    deadline = self._deadline()
    profiles = self._list_profiles(deadline)
    active = require_one_active_profile(profiles)
    target = next((profile for profile in profiles if profile.iccid == iccid), None)
    if target is None:
      raise LPAProfileNotFoundError(f"profile not found: ***{iccid[-4:]}")
    if active.iccid == iccid:
      return active

    failure: Exception | None = None
    try:
      with self._prepared_mutation_channel(deadline):
        code = self._enable_profile(iccid, deadline)
      if code == PROFILE_CAT_BUSY:
        failure = LPABusy("EnableProfile returned catBusy; mutation was not reissued")
      elif code != PROFILE_OK:
        failure = LPAError(f"EnableProfile failed: {PROFILE_ERROR_CODES.get(code, 'unknown')} (0x{code:02X}); not reissued")
    except Exception as error:
      failure = error

    try:
      observed = require_one_active_profile(self._list_profiles(deadline))
    except Exception:
      observed = None
    if failure is not None:
      if isinstance(failure, (LPABusy, LPAMutationAmbiguous, LPAError)):
        setattr(failure, "observed_active_iccid", observed.iccid if observed else "")
        raise failure
      raise LPAError(f"EnableProfile stopped without reissue: {type(failure).__name__}: {failure}") from None
    if observed is None or observed.iccid != iccid:
      raise LPAMutationAmbiguous(
        f"EnableProfile returned success but read-only eUICC verification did not confirm ***{iccid[-4:]}")
    return observed

  def switch_profile(self, iccid: str) -> None:
    self.prepare_and_switch_profile(iccid)

  def is_euicc(self) -> bool:
    # +CCHO:<n> -> ISD-R applet present, eUICC. Any error -> non-eUICC.
    deadline = self._deadline()
    with self._acquire_lock(deadline):
      try:
        lines = self._client.query(f'AT+CCHO="{ISDR_AID}"', deadline=deadline)
      except (RuntimeError, TimeoutError, LPADeadlineExceeded):
        return False
      for line in lines:
        if line.startswith("+CCHO:") and (ch := line.split(":", 1)[1].strip()):
          try:
            self._client.query(f"AT+CCHC={ch}", deadline=deadline)
          except (RuntimeError, TimeoutError, LPADeadlineExceeded):
            pass
          self._client.channel = None
          return True
      return False
