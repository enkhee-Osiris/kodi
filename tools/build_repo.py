#!/usr/bin/env python3
"""tools/build_repo.py — rebuilds this Kodi repo from sources.yaml.

The repository is tiered: each Kodi-version tier (e.g. omega, piers) gets
its own addons.xml/addons.xml.md5 under zips/tiers/<tier>/, matching a
<dir minversion=... maxversion=...> block in repository.osiris/addon.xml.
The actual per-addon zips are stored ONCE in a shared zips/<id>/ store and
referenced by whichever tier(s) need that exact version -- if two tiers
resolve to the same version (as is common when an addon isn't split per
Kodi version upstream), only one physical copy is kept on disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import xml.etree.ElementTree as ET

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT / "sources.yaml"
ZIPS_DIR = ROOT / "zips"
TIERS_DIR = ZIPS_DIR / "tiers"

REPO_ADDON_ID = "repository.osiris"
REPO_ADDON_DIR = ROOT / "repository.osiris"

HTTP_TIMEOUT = 30
VALID_TYPES = {"kodi_repo", "github", "zip_url"}


class BuildError(Exception):
    pass


class SourceConfigError(BuildError):
    pass


class AddonIdMismatch(BuildError):
    pass


class UpstreamNotFound(BuildError):
    pass


@dataclass
class AddonSource:
    id: str
    type: str
    raw: dict


@dataclass
class Tier:
    name: str
    minversion: str
    maxversion: Optional[str]
    addons: list[AddonSource] = field(default_factory=list)


@dataclass
class ProcessResult:
    id: str
    status: str  # "unchanged" | "updated" | "added" | "removed" | "failed"
    detail: str = ""
    tier: Optional[str] = None


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {"User-Agent": "osiris-repo-builder/1.0 (+https://github.com/enkhee-Osiris/kodi)"}
    )
    return session


def load_tiers(path: Path) -> list[Tier]:
    if not path.exists():
        raise SourceConfigError(f"{path} does not exist")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tier_entries = data.get("tiers")
    if not isinstance(tier_entries, list) or not tier_entries:
        raise SourceConfigError("sources.yaml must have a non-empty top-level 'tiers:' list")

    tiers: list[Tier] = []
    seen_tier_names = set()
    for ti, tier_entry in enumerate(tier_entries):
        if not isinstance(tier_entry, dict):
            raise SourceConfigError(f"tiers[{ti}] is not a mapping")
        name = tier_entry.get("name")
        minversion = tier_entry.get("minversion")
        maxversion = tier_entry.get("maxversion")
        if not name or not isinstance(name, str):
            raise SourceConfigError(f"tiers[{ti}] missing required string 'name'")
        if name in seen_tier_names:
            raise SourceConfigError(f"duplicate tier name '{name}' in sources.yaml")
        seen_tier_names.add(name)
        if not minversion or not isinstance(minversion, str):
            raise SourceConfigError(f"tiers[{ti}] ('{name}') missing required string 'minversion'")
        if maxversion is not None and not isinstance(maxversion, str):
            raise SourceConfigError(f"tiers[{ti}] ('{name}') 'maxversion' must be a string if set")

        entries = tier_entry.get("addons")
        if not isinstance(entries, list):
            raise SourceConfigError(f"tiers[{ti}] ('{name}') must have an 'addons:' list")

        addons: list[AddonSource] = []
        seen_ids = set()
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise SourceConfigError(f"tiers[{ti}] ('{name}') addons[{i}] is not a mapping")
            addon_id = entry.get("id")
            addon_type = entry.get("type")
            if not addon_id or not isinstance(addon_id, str):
                raise SourceConfigError(f"tiers[{ti}] ('{name}') addons[{i}] missing required string 'id'")
            if addon_id == REPO_ADDON_ID:
                raise SourceConfigError(
                    f"tiers[{ti}] ('{name}') addons[{i}]: '{REPO_ADDON_ID}' is built "
                    f"automatically; remove it from sources.yaml"
                )
            if addon_type not in VALID_TYPES:
                raise SourceConfigError(
                    f"tiers[{ti}] ('{name}') addons[{i}] ('{addon_id}') has invalid type '{addon_type}'"
                )
            if addon_id in seen_ids:
                raise SourceConfigError(f"duplicate addon id '{addon_id}' within tier '{name}'")
            seen_ids.add(addon_id)
            addons.append(AddonSource(id=addon_id, type=addon_type, raw=entry))

        tiers.append(Tier(name=name, minversion=minversion, maxversion=maxversion, addons=addons))
    return tiers


def parse_version_key(version: str):
    """Debian-style sortable key: alternating digit/text runs; digit runs
    compare numerically, text runs lexicographically, a run absent in one
    side sorts lower than any run present (so 1.2.3 < 1.2.3+matrix.1)."""
    parts = re.findall(r"\d+|\D+", version.strip())
    key = []
    for part in parts:
        if part.isdigit():
            key.append((1, int(part), ""))
        else:
            key.append((0, 0, part))
    return tuple(key)


def read_addon_xml_from_zip(zip_path: Path, expected_id: Optional[str] = None):
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        top_dirs = {n.split("/", 1)[0] for n in names if "/" in n}
        candidates = [n for n in names if n.count("/") == 1 and n.endswith("/addon.xml")]
        if len(top_dirs) != 1 or not candidates:
            raise BuildError(
                f"{zip_path.name}: not shaped like a Kodi addon zip "
                f"(expected one top-level folder containing addon.xml)"
            )
        addon_xml_name = candidates[0]
        top_dir = addon_xml_name.split("/", 1)[0]
        xml_bytes = zf.read(addon_xml_name)
    element = ET.fromstring(xml_bytes)
    addon_id = element.get("id")
    version = element.get("version")
    if not addon_id or not version:
        raise BuildError(f"{zip_path.name}: addon.xml missing id/version")
    if top_dir != addon_id:
        raise BuildError(f"{zip_path.name}: top-level folder '{top_dir}' != addon id '{addon_id}'")
    if expected_id and addon_id != expected_id:
        raise AddonIdMismatch(f"expected addon id '{expected_id}' but zip contains '{addon_id}'")
    return addon_id, version, element


def read_addon_xml_from_dir(dir_path: Path, expected_id: Optional[str] = None):
    addon_xml = dir_path / "addon.xml"
    if not addon_xml.exists():
        raise BuildError(f"{dir_path}: no addon.xml found")
    element = ET.fromstring(addon_xml.read_bytes())
    addon_id = element.get("id")
    version = element.get("version")
    if not addon_id or not version:
        raise BuildError(f"{addon_xml}: missing id/version")
    if expected_id and addon_id != expected_id:
        raise AddonIdMismatch(f"expected addon id '{expected_id}' but {addon_xml} contains '{addon_id}'")
    return addon_id, version, element


def write_deterministic_zip(
    dest: Path, addon_id: str, root_dir: Path, exclude: frozenset[str] = frozenset()
) -> None:
    """exclude holds top-level entry names (relative to root_dir) to omit --
    e.g. a repo's tests/CI files when the addon source lives at repo root
    alongside non-addon development files."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_name(dest.name + ".tmp")
    files = sorted(
        p
        for p in root_dir.rglob("*")
        if p.is_file() and p.relative_to(root_dir).parts[0] not in exclude
    )
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in files:
            arcname = f"{addon_id}/{file_path.relative_to(root_dir).as_posix()}"
            zinfo = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zinfo.external_attr = 0o644 << 16
            zf.writestr(zinfo, file_path.read_bytes())
    tmp_path.replace(dest)


def current_zip_info(addon_id: str) -> tuple[Optional[str], Optional[Path]]:
    """Version+path of the (single, non-tiered) zip on disk for addon_id,
    or (None, None) if absent or unopenable. Only used for repository.osiris,
    which is not tiered -- one build serves every Kodi version."""
    addon_dir = ZIPS_DIR / addon_id
    if not addon_dir.exists():
        return None, None
    for candidate in sorted(addon_dir.glob(f"{addon_id}-*.zip"), reverse=True):
        try:
            _, version, _ = read_addon_xml_from_zip(candidate, expected_id=addon_id)
            return version, candidate
        except BuildError:
            continue
    return None, None


def replace_addon_zip(addon_id: str, new_zip_source: Path) -> Path:
    _, version, _ = read_addon_xml_from_zip(new_zip_source, expected_id=addon_id)
    addon_dir = ZIPS_DIR / addon_id
    addon_dir.mkdir(parents=True, exist_ok=True)
    for old in addon_dir.glob(f"{addon_id}-*.zip"):
        old.unlink()
    final_path = addon_dir / f"{addon_id}-{version}.zip"
    shutil.copyfile(new_zip_source, final_path)
    return final_path


def _download(session: requests.Session, url: str, dest: Path) -> None:
    with session.get(url, timeout=HTTP_TIMEOUT, stream=True) as resp:
        resp.raise_for_status()
        tmp_path = dest.with_name(dest.name + ".part")
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        tmp_path.replace(dest)


def resolve_local(addon_id: str, dir_path: Path, tmp: Path) -> tuple[str, Path]:
    _, version, _ = read_addon_xml_from_dir(dir_path, expected_id=addon_id)
    zip_path = tmp / f"{addon_id}.zip"
    write_deterministic_zip(zip_path, addon_id, dir_path)
    return version, zip_path


def resolve_kodi_repo(source: AddonSource, session: requests.Session, tmp: Path) -> tuple[str, Path]:
    addons_xml_url = source.raw.get("repo_addons_xml_url")
    datadir_url = source.raw.get("repo_datadir_url")
    if not addons_xml_url or not datadir_url:
        raise SourceConfigError(f"{source.id}: kodi_repo requires repo_addons_xml_url and repo_datadir_url")

    resp = session.get(addons_xml_url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    element = root.find(f"./addon[@id='{source.id}']")
    if element is None:
        raise UpstreamNotFound(f"{source.id}: not found in upstream addons.xml at {addons_xml_url}")
    version = element.get("version")
    if not version:
        raise UpstreamNotFound(f"{source.id}: upstream addon entry has no version")

    zip_url = f"{datadir_url.rstrip('/')}/{source.id}/{source.id}-{version}.zip"
    zip_path = tmp / f"{source.id}.zip"
    _download(session, zip_url, zip_path)
    read_addon_xml_from_zip(zip_path, expected_id=source.id)
    return version, zip_path


def resolve_github(source: AddonSource, session: requests.Session, tmp: Path) -> tuple[str, Path]:
    repo = source.raw.get("github_repo")
    branch = source.raw.get("branch", "main")
    subdir = source.raw.get("subdir", "").strip("/")
    if not repo:
        raise SourceConfigError(f"{source.id}: github requires github_repo")

    archive_url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
    archive_path = tmp / "archive.zip"
    _download(session, archive_url, archive_path)

    extract_dir = tmp / "extracted"
    with zipfile.ZipFile(archive_path) as zf:
        zf.extractall(extract_dir)

    top_level = next(extract_dir.iterdir())
    addon_dir = (top_level / subdir) if subdir else top_level
    _, version, _ = read_addon_xml_from_dir(addon_dir, expected_id=source.id)

    exclude = frozenset(source.raw.get("exclude", []))
    zip_path = tmp / f"{source.id}.zip"
    write_deterministic_zip(zip_path, source.id, addon_dir, exclude=exclude)
    return version, zip_path


def resolve_zip_url(source: AddonSource, session: requests.Session, tmp: Path) -> tuple[str, Path]:
    url = source.raw.get("url")
    if not url:
        raise SourceConfigError(f"{source.id}: zip_url requires url")
    zip_path = tmp / f"{source.id}.zip"
    _download(session, url, zip_path)
    _, version, _ = read_addon_xml_from_zip(zip_path, expected_id=source.id)
    return version, zip_path


RESOLVERS = {
    "kodi_repo": resolve_kodi_repo,
    "github": resolve_github,
    "zip_url": resolve_zip_url,
}


def _config_key(source: AddonSource) -> str:
    return json.dumps(source.raw, sort_keys=True, default=str)


def ensure_addon_zip(
    source: AddonSource, session: requests.Session, cache: dict
) -> tuple[str, Path]:
    """Resolves source and ensures its zip exists in the SHARED zips/<id>/
    store (existence-checked by exact version -- multiple versions of the
    same id can coexist if different tiers need different versions).
    Memoized per exact source config within this run: two tiers with a
    byte-identical source config (common when an addon isn't split per
    Kodi version upstream) only hit the network once."""
    key = _config_key(source)
    if key in cache:
        result = cache[key]
        if isinstance(result, BuildError):
            raise result
        return result

    try:
        with tempfile.TemporaryDirectory(prefix=f"osiris-{source.id}-") as tmp_str:
            version, tmp_zip = RESOLVERS[source.type](source, session, Path(tmp_str))
            target = ZIPS_DIR / source.id / f"{source.id}-{version}.zip"
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(tmp_zip, target)
        cache[key] = (version, target)
        return version, target
    except (BuildError, requests.RequestException) as exc:
        cache[key] = exc if isinstance(exc, BuildError) else BuildError(str(exc))
        raise


def snapshot_existing_versions() -> dict[str, set[str]]:
    """{addon_id: {versions already on disk}} before this run's mutations --
    used to classify added/updated/unchanged without needing to parse each
    tier's previous addons.xml."""
    snapshot: dict[str, set[str]] = {}
    if not ZIPS_DIR.exists():
        return snapshot
    for addon_dir in ZIPS_DIR.iterdir():
        if not addon_dir.is_dir() or addon_dir.name == "tiers":
            continue
        versions = set()
        for zip_path in addon_dir.glob(f"{addon_dir.name}-*.zip"):
            try:
                _, version, _ = read_addon_xml_from_zip(zip_path, expected_id=addon_dir.name)
                versions.add(version)
            except BuildError:
                continue
        if versions:
            snapshot[addon_dir.name] = versions
    return snapshot


def classify(addon_id: str, version: str, existing_before: dict[str, set[str]]) -> str:
    prior = existing_before.get(addon_id, set())
    if version in prior:
        return "unchanged"
    return "added" if not prior else "updated"


def read_tier_addon_versions(tier_name: str) -> dict[str, str]:
    """{addon_id: version} this tier's addons.xml advertised before this
    run -- used as a fallback when a source fails to resolve this run, so
    a single dead upstream link doesn't drop the addon from the tier."""
    addons_xml_path = TIERS_DIR / tier_name / "addons.xml"
    if not addons_xml_path.exists():
        return {}
    try:
        root = ET.fromstring(addons_xml_path.read_bytes())
    except ET.ParseError:
        return {}
    versions: dict[str, str] = {}
    for a in root.findall("addon"):
        addon_id, version = a.get("id"), a.get("version")
        if addon_id and version and addon_id != REPO_ADDON_ID:
            versions[addon_id] = version
    return versions


def process_repo_addon(existing_before: dict[str, set[str]]) -> ProcessResult:
    try:
        with tempfile.TemporaryDirectory(prefix="osiris-repo-") as tmp_str:
            new_version, new_zip_path = resolve_local(REPO_ADDON_ID, REPO_ADDON_DIR, Path(tmp_str))
            target = ZIPS_DIR / REPO_ADDON_ID / f"{REPO_ADDON_ID}-{new_version}.zip"
            if not target.exists():
                if target.parent.exists():
                    for old in target.parent.glob(f"{REPO_ADDON_ID}-*.zip"):
                        old.unlink()
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(new_zip_path, target)
        status = classify(REPO_ADDON_ID, new_version, existing_before)
        prior = sorted(existing_before.get(REPO_ADDON_ID, set())) or "None"
        detail = new_version if status == "unchanged" else f"{prior} -> {new_version}"
        return ProcessResult(REPO_ADDON_ID, status, detail)
    except BuildError as exc:
        return ProcessResult(REPO_ADDON_ID, "failed", str(exc))


def process_tier(
    tier: Tier, session: requests.Session, cache: dict, existing_before: dict[str, set[str]]
) -> tuple[dict[str, str], list[ProcessResult]]:
    """Returns ({addon_id: version to publish in this tier's addons.xml},
    [ProcessResult per addon in this tier])."""
    previous = read_tier_addon_versions(tier.name)
    resolved: dict[str, str] = {}
    results: list[ProcessResult] = []

    for source in tier.addons:
        try:
            version, _ = ensure_addon_zip(source, session, cache)
            resolved[source.id] = version
            status = classify(source.id, version, existing_before)
            prior = sorted(existing_before.get(source.id, set())) or "None"
            detail = version if status == "unchanged" else f"{prior} -> {version}"
            results.append(ProcessResult(source.id, status, detail, tier=tier.name))
        except (BuildError, requests.RequestException) as exc:
            fallback = previous.get(source.id)
            if fallback:
                resolved[source.id] = fallback
                results.append(
                    ProcessResult(
                        source.id, "failed", f"{exc} (kept last-known-good {fallback})", tier=tier.name
                    )
                )
            else:
                results.append(ProcessResult(source.id, "failed", str(exc), tier=tier.name))

    return resolved, results


def prune_stale_versions(needed: dict[str, set[str]]) -> list[ProcessResult]:
    """Removes zip files/folders no longer referenced by ANY tier's
    resolved version set -- multiple versions of the same id are kept
    side by side as long as some tier still needs each one."""
    results = []
    if not ZIPS_DIR.exists():
        return results
    for addon_dir in sorted(ZIPS_DIR.iterdir()):
        if not addon_dir.is_dir() or addon_dir.name == "tiers":
            continue
        addon_id = addon_dir.name
        if addon_id not in needed:
            shutil.rmtree(addon_dir)
            results.append(ProcessResult(addon_id, "removed", "no longer referenced by any tier"))
            continue
        for zip_path in sorted(addon_dir.glob(f"{addon_id}-*.zip")):
            try:
                _, version, _ = read_addon_xml_from_zip(zip_path, expected_id=addon_id)
            except BuildError:
                zip_path.unlink()
                results.append(ProcessResult(addon_id, "removed", f"corrupt/unreadable {zip_path.name}"))
                continue
            if version not in needed[addon_id]:
                zip_path.unlink()
                results.append(ProcessResult(addon_id, "removed", f"stale version {version}"))
    return results


def prune_stale_tiers(known_tier_names: set[str]) -> None:
    if not TIERS_DIR.exists():
        return
    for tier_dir in TIERS_DIR.iterdir():
        if tier_dir.is_dir() and tier_dir.name not in known_tier_names:
            shutil.rmtree(tier_dir)


def regenerate_tier_addons_xml(tier_name: str, id_versions: dict[str, str]) -> None:
    entries = []

    _repo_version, repo_zip = current_zip_info(REPO_ADDON_ID)
    if repo_zip is not None:
        try:
            found_id, _v, element = read_addon_xml_from_zip(repo_zip, expected_id=REPO_ADDON_ID)
            entries.append((found_id, element))
        except BuildError as exc:
            print(f"::warning::[{tier_name}] skipping {repo_zip}: {exc}", file=sys.stderr)

    for addon_id, version in id_versions.items():
        zip_path = ZIPS_DIR / addon_id / f"{addon_id}-{version}.zip"
        try:
            found_id, _v, element = read_addon_xml_from_zip(zip_path, expected_id=addon_id)
            entries.append((found_id, element))
        except BuildError as exc:
            print(f"::warning::[{tier_name}] skipping {zip_path}: {exc}", file=sys.stderr)

    entries.sort(key=lambda pair: pair[0])

    root = ET.Element("addons")
    for _, element in entries:
        root.append(element)
    ET.indent(root, space="  ")
    xml_bytes = ET.tostring(root, encoding="UTF-8", xml_declaration=True) + b"\n"

    tier_dir = TIERS_DIR / tier_name
    tier_dir.mkdir(parents=True, exist_ok=True)
    addons_xml_path = tier_dir / "addons.xml"
    addons_md5_path = tier_dir / "addons.xml.md5"

    tmp_xml = addons_xml_path.with_suffix(".xml.tmp")
    tmp_xml.write_bytes(xml_bytes)
    tmp_xml.replace(addons_xml_path)

    digest = hashlib.md5(addons_xml_path.read_bytes()).hexdigest()
    tmp_md5 = addons_md5_path.with_suffix(".md5.tmp")
    tmp_md5.write_text(digest, encoding="utf-8")
    tmp_md5.replace(addons_md5_path)


def write_job_summary(results: list[ProcessResult]) -> None:
    failed = [r for r in results if r.status == "failed"]
    changed = [r for r in results if r.status in ("added", "updated", "removed")]

    lines = ["| Tier | Addon | Status | Detail |", "| --- | --- | --- | --- |"]
    for r in results:
        lines.append(f"| {r.tier or '-'} | `{r.id}` | {r.status} | {r.detail} |")
    summary = "\n".join(lines)

    print(summary)
    for r in failed:
        print(f"::warning::[{r.tier or '-'}] {r.id} failed to update: {r.detail}")

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write("## Osiris repo build summary\n\n")
            f.write(summary + "\n\n")
            if failed:
                f.write(f"**{len(failed)} source(s) failed this run** (see warnings above).\n\n")
            f.write(
                f"{len(changed)} entries added/updated/removed, {len(failed)} failed, "
                f"{len(results) - len(changed) - len(failed)} unchanged.\n"
            )

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"had_failures={'true' if failed else 'false'}\n")


def main() -> int:
    existing_before = snapshot_existing_versions()
    results = [process_repo_addon(existing_before)]

    try:
        tiers = load_tiers(SOURCES_FILE)
    except SourceConfigError as exc:
        print(f"::error::sources.yaml is invalid: {exc}", file=sys.stderr)
        return 1

    session = make_session()
    cache: dict = {}
    needed: dict[str, set[str]] = {}
    per_tier_versions: dict[str, dict[str, str]] = {}

    for tier in tiers:
        tier_versions, tier_results = process_tier(tier, session, cache, existing_before)
        per_tier_versions[tier.name] = tier_versions
        results.extend(tier_results)
        for addon_id, version in tier_versions.items():
            needed.setdefault(addon_id, set()).add(version)

    repo_version, _ = current_zip_info(REPO_ADDON_ID)
    if repo_version:
        needed.setdefault(REPO_ADDON_ID, set()).add(repo_version)

    results.extend(prune_stale_versions(needed))
    prune_stale_tiers({t.name for t in tiers})

    for tier in tiers:
        regenerate_tier_addons_xml(tier.name, per_tier_versions[tier.name])

    write_job_summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
