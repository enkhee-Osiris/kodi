#!/usr/bin/env python3
"""tools/build_repo.py — rebuilds this Kodi repo from sources.yaml."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
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
ADDONS_XML_PATH = ZIPS_DIR / "addons.xml"
ADDONS_XML_MD5_PATH = ZIPS_DIR / "addons.xml.md5"

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
class ProcessResult:
    id: str
    status: str  # "unchanged" | "updated" | "added" | "removed" | "failed"
    detail: str = ""


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


def load_sources(path: Path) -> list[AddonSource]:
    if not path.exists():
        raise SourceConfigError(f"{path} does not exist")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("addons")
    if not isinstance(entries, list):
        raise SourceConfigError("sources.yaml must have a top-level 'addons:' list")

    sources: list[AddonSource] = []
    seen_ids = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SourceConfigError(f"addons[{i}] is not a mapping")
        addon_id = entry.get("id")
        addon_type = entry.get("type")
        if not addon_id or not isinstance(addon_id, str):
            raise SourceConfigError(f"addons[{i}] missing required string 'id'")
        if addon_id == REPO_ADDON_ID:
            raise SourceConfigError(
                f"addons[{i}]: '{REPO_ADDON_ID}' is built automatically; remove it from sources.yaml"
            )
        if addon_type not in VALID_TYPES:
            raise SourceConfigError(f"addons[{i}] ('{addon_id}') has invalid type '{addon_type}'")
        if addon_id in seen_ids:
            raise SourceConfigError(f"duplicate addon id '{addon_id}' in sources.yaml")
        seen_ids.add(addon_id)
        sources.append(AddonSource(id=addon_id, type=addon_type, raw=entry))
    return sources


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


def is_newer(new_version: str, old_version: Optional[str]) -> bool:
    if old_version is None:
        return True
    if new_version == old_version:
        return False
    try:
        return parse_version_key(new_version) > parse_version_key(old_version)
    except Exception:
        return True


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


def write_deterministic_zip(dest: Path, addon_id: str, root_dir: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_name(dest.name + ".tmp")
    files = sorted(p for p in root_dir.rglob("*") if p.is_file())
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in files:
            arcname = f"{addon_id}/{file_path.relative_to(root_dir).as_posix()}"
            zinfo = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zinfo.external_attr = 0o644 << 16
            zf.writestr(zinfo, file_path.read_bytes())
    tmp_path.replace(dest)


def current_zip_info(addon_id: str) -> tuple[Optional[str], Optional[Path]]:
    """Version+path of the zip on disk for addon_id, or (None, None) if
    absent or unopenable (self-healing against a corrupted leftover)."""
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

    zip_path = tmp / f"{source.id}.zip"
    write_deterministic_zip(zip_path, source.id, addon_dir)
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


def process_addon(source: AddonSource, session: requests.Session) -> ProcessResult:
    current_version, _ = current_zip_info(source.id)
    try:
        with tempfile.TemporaryDirectory(prefix=f"osiris-{source.id}-") as tmp_str:
            tmp = Path(tmp_str)
            new_version, new_zip_path = RESOLVERS[source.type](source, session, tmp)
            if not is_newer(new_version, current_version):
                return ProcessResult(source.id, "unchanged", f"{current_version}")
            replace_addon_zip(source.id, new_zip_path)
            status = "added" if current_version is None else "updated"
            return ProcessResult(source.id, status, f"{current_version} -> {new_version}")
    except (BuildError, requests.RequestException) as exc:
        return ProcessResult(source.id, "failed", str(exc))


def process_repo_addon() -> ProcessResult:
    current_version, _ = current_zip_info(REPO_ADDON_ID)
    try:
        with tempfile.TemporaryDirectory(prefix="osiris-repo-") as tmp_str:
            new_version, new_zip_path = resolve_local(REPO_ADDON_ID, REPO_ADDON_DIR, Path(tmp_str))
            if not is_newer(new_version, current_version):
                return ProcessResult(REPO_ADDON_ID, "unchanged", f"{current_version}")
            replace_addon_zip(REPO_ADDON_ID, new_zip_path)
            status = "added" if current_version is None else "updated"
            return ProcessResult(REPO_ADDON_ID, status, f"{current_version} -> {new_version}")
    except BuildError as exc:
        return ProcessResult(REPO_ADDON_ID, "failed", str(exc))


def prune_orphaned_addons(known_ids: set[str]) -> list[ProcessResult]:
    results = []
    if not ZIPS_DIR.exists():
        return results
    for addon_dir in sorted(ZIPS_DIR.iterdir()):
        if addon_dir.is_dir() and addon_dir.name not in known_ids:
            shutil.rmtree(addon_dir)
            results.append(ProcessResult(addon_dir.name, "removed", "no longer in sources.yaml"))
    return results


def regenerate_addons_xml() -> None:
    entries = []
    for zip_path in sorted(ZIPS_DIR.glob("*/*.zip")):
        addon_id = zip_path.parent.name
        try:
            found_id, _version, element = read_addon_xml_from_zip(zip_path, expected_id=addon_id)
            entries.append((found_id, element))
        except BuildError as exc:
            print(f"::warning::skipping {zip_path}: {exc}", file=sys.stderr)

    entries.sort(key=lambda pair: pair[0])

    root = ET.Element("addons")
    for _, element in entries:
        root.append(element)
    ET.indent(root, space="  ")
    xml_bytes = ET.tostring(root, encoding="UTF-8", xml_declaration=True) + b"\n"

    ADDONS_XML_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_xml = ADDONS_XML_PATH.with_suffix(".xml.tmp")
    tmp_xml.write_bytes(xml_bytes)
    tmp_xml.replace(ADDONS_XML_PATH)

    digest = hashlib.md5(ADDONS_XML_PATH.read_bytes()).hexdigest()
    tmp_md5 = ADDONS_XML_MD5_PATH.with_suffix(".md5.tmp")
    tmp_md5.write_text(digest, encoding="utf-8")
    tmp_md5.replace(ADDONS_XML_MD5_PATH)


def write_job_summary(results: list[ProcessResult]) -> None:
    failed = [r for r in results if r.status == "failed"]
    changed = [r for r in results if r.status in ("added", "updated", "removed")]

    lines = ["| Addon | Status | Detail |", "| --- | --- | --- |"]
    for r in results:
        lines.append(f"| `{r.id}` | {r.status} | {r.detail} |")
    summary = "\n".join(lines)

    print(summary)
    for r in failed:
        print(f"::warning::{r.id} failed to update: {r.detail}")

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write("## Osiris repo build summary\n\n")
            f.write(summary + "\n\n")
            if failed:
                f.write(f"**{len(failed)} source(s) failed this run** (see warnings above).\n\n")
            f.write(
                f"{len(changed)} addon(s) added/updated/removed, {len(failed)} failed, "
                f"{len(results) - len(changed) - len(failed)} unchanged.\n"
            )

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"had_failures={'true' if failed else 'false'}\n")


def main() -> int:
    results = [process_repo_addon()]

    try:
        sources = load_sources(SOURCES_FILE)
    except SourceConfigError as exc:
        print(f"::error::sources.yaml is invalid: {exc}", file=sys.stderr)
        return 1

    session = make_session()
    for source in sources:
        results.append(process_addon(source, session))

    known_ids = {REPO_ADDON_ID} | {s.id for s in sources}
    results.extend(prune_orphaned_addons(known_ids))

    regenerate_addons_xml()
    write_job_summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
