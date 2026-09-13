#!/usr/bin/env python3
"""Offline, value-suppressing scanner for an explicit eSIM commit candidate."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Iterable


ACTIVATION_RE = re.compile(rb"LPA:[0-9]+\$([^$\s]+)\$[^\s]+", re.IGNORECASE)
ACTIVATION_URL_RE = re.compile(rb"LPA%3A[0-9]+%24", re.IGNORECASE)
IDENTIFIER_RE = re.compile(rb"(?<![0-9])[0-9]{18,32}(?![0-9])")


def secret_variants(secret_file: Path | None) -> tuple[bytes, ...]:
  if secret_file is None:
    return ()
  content = secret_file.read_bytes().strip()
  values = []
  for line in content.splitlines():
    value = line.strip()
    activation = b"LPA:" in value.upper() or b"LPA%3A" in value.upper()
    diverse_token = (len(value) >= 16 and len(set(value)) >= 8 and
                     any(65 <= byte <= 90 or 97 <= byte <= 122 for byte in value) and
                     any(48 <= byte <= 57 for byte in value))
    if activation or diverse_token:
      values.append(value)
  if b"\n" not in content and b"\r" not in content and len(content) >= 16:
    values.append(content)
  variants: set[bytes] = set(values)
  for value in values:
    variants.add(urllib.parse.quote_from_bytes(value, safe="").encode("ascii"))
    variants.add(urllib.parse.quote_plus(value).encode("ascii"))
  return tuple(value for value in variants if value)


def scan_bytes(data: bytes, exact: tuple[bytes, ...]) -> Counter[str]:
  findings: Counter[str] = Counter()
  findings["exact_private_value"] = sum(data.count(value) for value in exact)
  decoded = urllib.parse.unquote_to_bytes(data)
  findings["url_decoded_private_value"] = sum(decoded.count(value) for value in exact)
  for match in ACTIVATION_RE.finditer(data):
    domain = match.group(1).lower().rstrip(b".")
    synthetic = domain.endswith(b".invalid") or domain in (b"example.com", b"example.net", b"example.org")
    synthetic = synthetic or any(domain.endswith(b"." + suffix) for suffix in
                                 (b"example.com", b"example.net", b"example.org"))
    if not synthetic:
      findings["live_activation_shape"] += 1
  findings["url_encoded_activation_shape"] = len(ACTIVATION_URL_RE.findall(data))
  findings["identifier_like"] = len(IDENTIFIER_RE.findall(data))
  return +findings


def iter_worktree(root: Path, excluded: set[Path]) -> Iterable[tuple[str, bytes]]:
  for directory, names, files in os.walk(root):
    names[:] = [name for name in names if name not in (".git", ".venv", "__pycache__")]
    for name in files:
      if name == ".git":
        continue
      path = Path(directory) / name
      try:
        resolved = path.resolve()
        if resolved in excluded or path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
          continue
        data = path.read_bytes()
      except OSError:
        continue
      if b"\x00" not in data[:8192]:
        yield str(path.relative_to(root)), data


def git_output(repo: Path, *args: str, input_data: bytes | None = None) -> bytes:
  environment = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}
  return subprocess.run(["git", "-C", str(repo), *args], input=input_data, capture_output=True,
                        check=True, env=environment).stdout


def iter_git_objects(repo: Path, objects: Iterable[tuple[str, str]], unavailable: list[str]) -> Iterable[tuple[str, bytes]]:
  environment = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}
  process = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=environment)
  if process.stdin is None or process.stdout is None:
    raise RuntimeError("failed to open git cat-file pipes")
  try:
    for oid, location in objects:
      process.stdin.write(f"{oid}\n".encode("ascii"))
      process.stdin.flush()
      header = process.stdout.readline().split()
      if len(header) == 2 and header[1] == b"missing":
        unavailable.append(location)
        continue
      if len(header) != 3:
        raise RuntimeError("invalid git cat-file response")
      size = int(header[2])
      data = process.stdout.read(size)
      if len(data) != size or process.stdout.read(1) != b"\n":
        raise RuntimeError("short git object read")
      if header[1] == b"blob" and size <= 16 * 1024 * 1024 and b"\x00" not in data[:8192]:
        yield location, data
  finally:
    process.stdin.close()
    process.stdout.close()
    if process.wait() != 0:
      raise RuntimeError("git cat-file failed")


def iter_index(repo: Path, unavailable: list[str]) -> Iterable[tuple[str, bytes]]:
  objects = []
  for entry in git_output(repo, "ls-files", "-s", "-z").split(b"\0"):
    if entry:
      metadata, path = entry.split(b"\t", 1)
      objects.append((metadata.split()[1].decode("ascii"), path.decode("utf-8", errors="replace")))
  yield from iter_git_objects(repo, objects, unavailable)


def iter_ancestry_objects(repo: Path, revision: str, unavailable: list[str]) -> Iterable[tuple[str, bytes]]:
  try:
    entries = git_output(repo, "rev-list", "--objects", revision).splitlines()
  except subprocess.CalledProcessError:
    unavailable.append("ancestry_unenumerable")
    return
  seen: set[str] = set()
  objects = []
  for entry in entries:
    parts = entry.decode("utf-8", errors="replace").split(" ", 1)
    oid = parts[0]
    if oid in seen:
      continue
    seen.add(oid)
    objects.append((oid, parts[1] if len(parts) == 2 else f"object:{oid[:12]}"))
  yield from iter_git_objects(repo, objects, unavailable)


def scan_scope(scope: str, items: Iterable[tuple[str, bytes]], exact: tuple[bytes, ...]) -> list[dict[str, object]]:
  results = []
  for location, data in items:
    findings = scan_bytes(data, exact)
    for rule, count in sorted(findings.items()):
      if count:
        results.append({"scope": scope, "location": location, "rule": rule, "count": count})
  return results


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description="offline scanner that never emits matched values or line excerpts")
  parser.add_argument("--repo", type=Path, required=True)
  parser.add_argument("--secret-file", type=Path)
  parser.add_argument("--revision", default="HEAD")
  parser.add_argument("--candidate-manifest", type=Path)
  parser.add_argument("--extra-file", action="append", default=[], type=Path,
                      help="additional explicit export artifact to scan; may be repeated")
  args = parser.parse_args(argv)
  repo = args.repo.resolve()
  secret_file = args.secret_file.resolve() if args.secret_file else None
  exact = secret_variants(secret_file)
  excluded = {secret_file} if secret_file else set()
  unavailable_index: list[str] = []
  unavailable_ancestry: list[str] = []
  findings = []
  findings.extend(scan_scope("working_tree", iter_worktree(repo, excluded), exact))
  findings.extend(scan_scope("index", iter_index(repo, unavailable_index), exact))
  findings.extend(scan_scope("ancestry", iter_ancestry_objects(repo, args.revision, unavailable_ancestry), exact))
  if args.candidate_manifest:
    paths = [line.strip() for line in args.candidate_manifest.read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    items = ((path, (repo / path).read_bytes()) for path in paths)
    findings.extend(scan_scope("candidate_export", items, exact))
  if args.extra_file:
    items = ((path.name, path.read_bytes()) for path in args.extra_file)
    findings.extend(scan_scope("review_export", items, exact))
  blocking_rules = {"exact_private_value", "url_decoded_private_value", "live_activation_shape",
                    "url_encoded_activation_shape"}
  blocking = [item for item in findings if item["rule"] in blocking_rules]
  identifier_summary = [{"scope": scope, "locations": len({item["location"] for item in findings
                                                             if item["scope"] == scope and item["rule"] == "identifier_like"}),
                         "count": sum(int(item["count"]) for item in findings
                                      if item["scope"] == scope and item["rule"] == "identifier_like")}
                        for scope in ("working_tree", "index", "ancestry", "candidate_export", "review_export")]
  reported = [item for item in findings if item["rule"] != "identifier_like"]
  incomplete = {"index_missing_objects": len(unavailable_index),
                "ancestry_missing_objects": len(unavailable_ancestry), "lazy_fetch_disabled": True}
  print(json.dumps({"blocking_findings": len(blocking), "findings": reported,
                    "identifier_heuristic_summary": identifier_summary,
                    "incomplete_scopes": incomplete,
                    "limits": "direct, URL-decoded, activation-shape, and decimal identifier heuristics only",
                    "matched_values_emitted": False}, indent=2, sort_keys=True))
  return 1 if blocking else (2 if unavailable_index or unavailable_ancestry else 0)


if __name__ == "__main__":
  raise SystemExit(main())
