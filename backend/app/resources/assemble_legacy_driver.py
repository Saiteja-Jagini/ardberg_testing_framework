"""Reconstruct a retired Playwright driver ZIP from its exact published dependencies.

Runs only inside the disposable runner. Mirrors the bundle layout documented by
https://github.com/microsoft/playwright-python/blob/main/scripts/build_driver.py
The historical Node pin comes from the npm package's upstream git revision.
"""
import base64
import hashlib
import io
import json
import platform
import re
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlsplit


def fetch(url):
    allowed = {"registry.npmjs.org", "raw.githubusercontent.com", "nodejs.org"}
    if urlsplit(url).scheme != "https" or urlsplit(url).hostname not in allowed:
        raise ValueError("Dependency URL is not an official source")
    with urllib.request.urlopen(url, timeout=60) as response:
        if urlsplit(response.url).hostname not in allowed:
            raise ValueError("Unexpected dependency redirect")
        data = response.read(150 * 1024 * 1024 + 1)
    if len(data) > 150 * 1024 * 1024:
        raise ValueError("Dependency archive exceeds limit")
    return data


def assemble(version):
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", version):
        raise ValueError("Invalid pinned driver version")
    metadata = json.loads(fetch(f"https://registry.npmjs.org/playwright-core/{version}"))
    if metadata["name"] != "playwright-core" or metadata["version"] != version:
        raise ValueError("Published driver version mismatch")
    commit = metadata["gitHead"]
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Missing immutable upstream revision")
    build_url = f"https://raw.githubusercontent.com/microsoft/playwright/{commit}/utils/build/build-playwright-driver.sh"
    build = fetch(build_url).decode()
    node_version = re.search(r'NODE_VERSION=["\'](\d+\.\d+\.\d+)["\']', build).group(1)
    package_url = metadata["dist"]["tarball"]
    if not package_url.startswith("https://registry.npmjs.org/playwright-core/-/"):
        raise ValueError("Unexpected driver package location")
    package = fetch(package_url)
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(package).digest()).decode()
    if metadata["dist"]["integrity"] != integrity:
        raise ValueError("Driver package integrity mismatch")
    arch = {"x86_64": "x64", "aarch64": "arm64"}[platform.machine()]
    node_name = f"node-v{node_version}-linux-{arch}"
    node_url = f"https://nodejs.org/dist/v{node_version}/{node_name}.tar.gz"
    node = fetch(node_url)
    checksums = fetch(f"https://nodejs.org/dist/v{node_version}/SHASUMS256.txt").decode()
    expected = next(line.split()[0] for line in checksums.splitlines() if line.split()[-1] == node_name + ".tar.gz")
    if hashlib.sha256(node).hexdigest() != expected:
        raise ValueError("Node binary integrity mismatch")
    suffix = "linux" if arch == "x64" else "linux-arm64"
    destination = Path("driver") / f"playwright-{version}-{suffix}.zip"
    destination.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ardberg-driver-") as temporary:
        root = Path(temporary)
        total = 0
        with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
            for member in archive.getmembers():
                target = (root / member.name).resolve()
                if (not member.name.startswith("package/") or root.resolve() not in target.parents
                        or member.issym() or member.islnk()):
                    raise ValueError("Unsafe driver package member")
                if not member.isfile():
                    continue
                total += member.size
                if total > 250 * 1024 * 1024:
                    raise ValueError("Expanded driver exceeds limit")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
        package_info = json.loads((root / "package/package.json").read_text())
        if package_info["version"] != version:
            raise ValueError("Extracted driver version mismatch")
        with tarfile.open(fileobj=io.BytesIO(node), mode="r:gz") as archive:
            (root / "node").write_bytes(archive.extractfile(node_name + "/bin/node").read())
            (root / "LICENSE").write_bytes(archive.extractfile(node_name + "/LICENSE").read())
        (root / "node").chmod(0o755)
        temporary_zip = destination.with_suffix(".zip.tmp")
        with zipfile.ZipFile(temporary_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
        temporary_zip.replace(destination)
    provenance = {"driver_version": version, "node_version": node_version,
                  "upstream_commit": commit, "package_integrity": integrity,
                  "package_url": package_url, "node_url": node_url, "node_sha256": expected,
                  "archive": destination.as_posix(), "archive_sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
    print(json.dumps(provenance))


if __name__ == "__main__":
    import sys
    assemble(sys.argv[1])
