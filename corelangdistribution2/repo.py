from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import tempfile
import time
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, as_completed
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Sequence

from . import __version__
from .chunker import iter_chunks, iter_file_chunks, parse_size, chunk_stats
from .codec import compress, decompress
from .hashutil import sha256_bytes, sha256_file
from .http_range import is_url, read_local_range, read_url, read_url_range, url_join, head_url, new_network_stats

EMPTY_SHA256 = sha256_bytes(b"")


class TokenBucket:
    """Small in-process token bucket used by alpha24 fetch workers.

    It is intentionally process-local, but unlike the alpha9 per-request sleep it
    is shared by all parallel chunk workers in a single fetch/extract run.
    """
    def __init__(self, rate_bps: int = 0):
        self.rate_bps = int(rate_bps or 0)
        self._lock = threading.Lock()
        self._next_free = time.monotonic()
        self.sleep_seconds = 0.0
        self.events = 0

    def consume(self, nbytes: int) -> None:
        if self.rate_bps <= 0 or nbytes <= 0:
            return
        delay = float(nbytes) / max(1, self.rate_bps)
        with self._lock:
            now = time.monotonic()
            wait_s = max(0.0, self._next_free - now)
            self._next_free = max(now, self._next_free) + delay
            if wait_s > 0:
                self.sleep_seconds += wait_s
                self.events += 1
        if wait_s > 0:
            time.sleep(wait_s)

    def snapshot(self) -> dict[str, Any]:
        return {"rate_bps": self.rate_bps, "sleep_seconds": round(self.sleep_seconds, 6), "events": self.events}


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_rel(path: Path, root: Path) -> str:
    rel = path.relative_to(root).as_posix()
    pp = PurePosixPath(rel)
    if pp.is_absolute() or ".." in pp.parts:
        raise ValueError(f"unsafe path: {rel}")
    return rel


def ensure_safe_member(path: str) -> PurePosixPath:
    pp = PurePosixPath(path)
    if pp.is_absolute() or ".." in pp.parts or str(pp) in ("", "."):
        raise ValueError(f"unsafe path: {path}")
    return pp

WINDOWS_RESERVED_BASENAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
WINDOWS_FORBIDDEN_CHARS = set('<>:"\\|?*')


def _portable_path_key(path: str, *, casefold: bool = True, unicode_form: str = "NFC") -> str:
    text = unicodedata.normalize(unicode_form, str(path).replace("\\", "/"))
    return text.casefold() if casefold else text


def path_portability_issues(path: str) -> Dict[str, list[str]]:
    """Return cross-platform path errors/warnings for a repo member path.

    CLD2 stores POSIX-style relative paths. Alpha15 keeps Linux-permissive pack
    behavior, but release-check flags names that will break or collide on common
    Windows/macOS clients.
    """
    errors: list[str] = []
    warnings: list[str] = []
    try:
        pp = ensure_safe_member(path)
    except Exception as e:
        return {"errors": [str(e)], "warnings": []}
    text = str(pp)
    if unicodedata.normalize("NFC", text) != text:
        warnings.append("path is not NFC-normalized; may collide on Unicode-normalizing filesystems")
    if len(text) > 240:
        warnings.append(f"path length {len(text)} may exceed legacy Windows/path tooling limits")
    for part in pp.parts:
        if not part or part in (".", ".."):
            errors.append(f"unsafe path component: {part!r}")
            continue
        if any(ord(ch) < 32 for ch in part):
            errors.append(f"control character in path component: {part!r}")
        bad = sorted(ch for ch in set(part) if ch in WINDOWS_FORBIDDEN_CHARS)
        if bad:
            errors.append(f"Windows-forbidden character(s) {''.join(bad)!r} in component: {part!r}")
        if part.endswith(" ") or part.endswith("."):
            errors.append(f"component has trailing space/dot and is not Windows-portable: {part!r}")
        base = part.split(".")[0].rstrip(" .").upper()
        if base in WINDOWS_RESERVED_BASENAMES:
            errors.append(f"Windows reserved device name in path component: {part!r}")
        if len(part.encode("utf-8")) > 255:
            warnings.append(f"component longer than 255 UTF-8 bytes: {part!r}")
    return {"errors": errors, "warnings": warnings}


def _apply_file_metadata(path: Path, fentry: Dict[str, Any]) -> None:
    """Best-effort restore of simple portable file metadata."""
    try:
        mode = fentry.get("mode")
        if mode is not None:
            os.chmod(path, int(mode) & 0o777)
    except Exception:
        pass
    try:
        mtime_ns = fentry.get("mtime_ns")
        if mtime_ns is not None:
            ns = int(mtime_ns)
            os.utime(path, ns=(ns, ns))
    except Exception:
        pass


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def read_json_local(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _require_ed25519():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
        from cryptography.hazmat.primitives import serialization
    except Exception as e:  # pragma: no cover - dependency/environment guard
        raise RuntimeError("Ed25519 signing requires the 'cryptography' package") from e
    return Ed25519PrivateKey, Ed25519PublicKey, serialization


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def _read_key_doc(path: str | Path) -> Dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError(f"empty key file: {path}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"alpha24 expects a JSON Ed25519 key file, not a raw alpha4 HMAC key: {path}") from e


def parse_iso_utc(value: str | None):
    from datetime import datetime, timezone
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utc_now_dt():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _time_check_window(*, not_before: str | None = None, expires_at: str | None = None, at_time: str | None = None) -> Dict[str, Any]:
    now_dt = parse_iso_utc(at_time) if at_time else utc_now_dt()
    if not_before:
        nb = parse_iso_utc(not_before)
        if nb and now_dt < nb:
            return {"ok": False, "error": f"not valid before {not_before}", "now": now_dt.isoformat().replace("+00:00", "Z")}
    if expires_at:
        exp = parse_iso_utc(expires_at)
        if exp and now_dt > exp:
            return {"ok": False, "error": f"expired at {expires_at}", "now": now_dt.isoformat().replace("+00:00", "Z")}
    return {"ok": True, "error": None, "now": now_dt.isoformat().replace("+00:00", "Z")}


def _release_time_status(repo: "LoadedRepo", at_time: str | None = None) -> Dict[str, Any]:
    return _time_check_window(not_before=repo.release.get("not_before"), expires_at=repo.release.get("expires_at"), at_time=at_time)


def _private_from_file(key_file: str | Path):
    Ed25519PrivateKey, _Ed25519PublicKey, _serialization = _require_ed25519()
    doc = _read_key_doc(key_file)
    if doc.get("algorithm") != "ed25519":
        raise ValueError(f"unsupported private key algorithm: {doc.get('algorithm')}")
    if doc.get("schema") != "CoreLangDistribution/PrivateKey":
        raise ValueError("signing requires a CoreLangDistribution private key JSON")
    private_raw = _b64d(str(doc.get("private_key", "")))
    return Ed25519PrivateKey.from_private_bytes(private_raw), doc


def _public_from_file(key_file: str | Path):
    _Ed25519PrivateKey, Ed25519PublicKey, _serialization = _require_ed25519()
    doc = _read_key_doc(key_file)
    if doc.get("algorithm") != "ed25519":
        raise ValueError(f"unsupported public key algorithm: {doc.get('algorithm')}")
    if doc.get("schema") == "CoreLangDistribution/PrivateKey":
        public_raw = _b64d(str(doc.get("public_key", "")))
    elif doc.get("schema") == "CoreLangDistribution/PublicKey":
        public_raw = _b64d(str(doc.get("public_key", "")))
    else:
        raise ValueError("trust key must be a CoreLangDistribution public key JSON")
    key_id = sha256_bytes(public_raw)[:16]
    if doc.get("key_id") and doc.get("key_id") != key_id:
        raise ValueError("public key_id does not match public key bytes")
    return Ed25519PublicKey.from_public_bytes(public_raw), doc, public_raw, key_id


def keygen(out_file: str | Path, pub_out: str | Path | None = None) -> Dict[str, Any]:
    Ed25519PrivateKey, _Ed25519PublicKey, serialization = _require_ed25519()
    out = Path(out_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_id = sha256_bytes(public_raw)[:16]
    created = now_iso()
    priv_doc = {
        "schema": "CoreLangDistribution/PrivateKey",
        "version": "2.0-alpha24",
        "algorithm": "ed25519",
        "key_id": key_id,
        "created_at": created,
        "private_key": _b64e(private_raw),
        "public_key": _b64e(public_raw),
        "warning": "Alpha11 development key. Keep private_key secret; distribute only the .pub.json trust key.",
    }
    pub_path = Path(pub_out) if pub_out else out.with_name(out.name + ".pub.json")
    pub_doc = {
        "schema": "CoreLangDistribution/PublicKey",
        "version": "2.0-alpha24",
        "algorithm": "ed25519",
        "key_id": key_id,
        "created_at": created,
        "public_key": _b64e(public_raw),
    }
    write_json(out, priv_doc)
    write_json(pub_path, pub_doc)
    try:
        out.chmod(0o600)
        pub_path.chmod(0o644)
    except Exception:
        pass
    return {"ok": True, "private_key_file": str(out), "public_key_file": str(pub_path), "key_id": key_id, "algorithm": "ed25519"}


def _repo_json(repo: "LoadedRepo", name: str) -> Any:
    if repo.is_remote:
        return json.loads(read_url(url_join(repo.source, name)).decode("utf-8"))
    return read_json_local(Path(repo.source) / name)


def _signature_payload(repo: "LoadedRepo") -> bytes:
    # Alpha11 public-key payload. It signs immutable metadata roots and index digests,
    # while deliberately excluding signatures.json itself.
    chunks_list = list(repo.chunks.values())
    payload = {
        "schema": "CoreLangDistribution/SignedReleasePayload",
        "payload_version": "2.0-alpha24",
        "release_id": repo.release.get("release_id"),
        "release_seq": int(repo.release.get("release_seq", 0)),
        "root_hash": repo.release.get("root_hash"),
        "expires_at": repo.release.get("expires_at"),
        "not_before": repo.release.get("not_before"),
        "files_sha256": sha256_bytes(json_bytes(repo.files)),
        "chunks_sha256": sha256_bytes(json_bytes(chunks_list)),
        "packs": repo.release.get("packs", []),
        "version": repo.release.get("version"),
    }
    return json_bytes(payload)


def sign_repo(repo_path: str | Path, key_file: str | Path) -> Dict[str, Any]:
    root = Path(repo_path)
    if is_url(str(repo_path)):
        raise ValueError("sign_repo expects a local .cldrepo path")
    repo = load_repo(root)
    private, key_doc = _private_from_file(key_file)
    _public, _doc, public_raw, key_id = _public_from_file(key_file)
    sig = private.sign(_signature_payload(repo))
    sig_doc = {
        "schema": "CoreLangDistribution/Signature",
        "version": "2.0-alpha24",
        "algorithm": "ed25519",
        "key_id": key_id,
        "public_key_sha256": sha256_bytes(public_raw),
        "signed_at": now_iso(),
        "signed_root_hash": repo.release.get("root_hash"),
        "signed_expires_at": repo.release.get("expires_at"),
        "signed_not_before": repo.release.get("not_before"),
        "signature": _b64e(sig),
        "note": "Alpha11 public-key trust slice with optional trusted-root policy. Replace development key management with a real release process before production.",
    }
    write_json(root / "signatures.json", sig_doc)
    release_path = root / "release.json"
    release = read_json_local(release_path)
    release["signatures"] = "signatures.json"
    write_json(release_path, release)
    return {"ok": True, "repo": str(root), "signature_file": str(root / "signatures.json"), "key_id": sig_doc["key_id"], "signed_root_hash": sig_doc["signed_root_hash"], "algorithm": "ed25519"}


def verify_repo_signature(source: str | Path, key_file: str | Path) -> Dict[str, Any]:
    repo = load_repo(source)
    sig_name = repo.release.get("signatures", "signatures.json")
    try:
        sig_doc = _repo_json(repo, sig_name)
    except Exception as e:
        return {"ok": False, "error": f"signature metadata missing or unreadable: {e}"}
    if sig_doc.get("algorithm") != "ed25519":
        return {"ok": False, "error": f"unsupported signature algorithm: {sig_doc.get('algorithm')}"}
    if sig_doc.get("signed_root_hash") != repo.release.get("root_hash"):
        return {"ok": False, "error": "signature signed_root_hash does not match release root_hash"}
    if sig_doc.get("signed_expires_at") != repo.release.get("expires_at"):
        return {"ok": False, "error": "signature signed_expires_at does not match release expires_at"}
    if sig_doc.get("signed_not_before") != repo.release.get("not_before"):
        return {"ok": False, "error": "signature signed_not_before does not match release not_before"}
    try:
        public, _doc, public_raw, key_id = _public_from_file(key_file)
        if sig_doc.get("key_id") != key_id:
            return {"ok": False, "algorithm": sig_doc.get("algorithm"), "key_id": sig_doc.get("key_id"), "trusted_key_id": key_id, "error": "signature key_id does not match trust key"}
        if sig_doc.get("public_key_sha256") and sig_doc.get("public_key_sha256") != sha256_bytes(public_raw):
            return {"ok": False, "algorithm": sig_doc.get("algorithm"), "key_id": sig_doc.get("key_id"), "trusted_key_id": key_id, "error": "signature public_key_sha256 does not match trust key"}
        public.verify(_b64d(str(sig_doc.get("signature", ""))), _signature_payload(repo))
    except Exception as e:
        return {"ok": False, "algorithm": sig_doc.get("algorithm"), "key_id": sig_doc.get("key_id"), "error": f"signature mismatch: {e}"}
    return {"ok": True, "algorithm": sig_doc.get("algorithm"), "key_id": sig_doc.get("key_id"), "signed_root_hash": sig_doc.get("signed_root_hash"), "error": None}


def root_init(
    out_file: str | Path,
    keys: list[str | Path],
    *,
    root_id: str = "default",
    expires_at: str | None = None,
    min_release_seq: int = 0,
    require_signature: bool = True,
    mirror_urls: Sequence[str] | None = None,
) -> Dict[str, Any]:
    """Create an alpha24 trusted-root document.

    This root is a local trust anchor distributed out-of-band. It is not itself
    signed in alpha17; beta should add root rotation and threshold signatures.
    """
    if not keys:
        raise ValueError("trusted root requires at least one public key")
    trusted = []
    seen = set()
    for k in keys:
        _pub, doc, public_raw, key_id = _public_from_file(k)
        if key_id in seen:
            continue
        seen.add(key_id)
        trusted.append({
            "key_id": key_id,
            "algorithm": "ed25519",
            "public_key": _b64e(public_raw),
            "public_key_sha256": sha256_bytes(public_raw),
            "source": str(k),
        })
    root_doc = {
        "schema": "CoreLangDistribution/TrustedRoot",
        "version": "2.0-alpha24",
        "root_id": root_id,
        "created_at": now_iso(),
        "expires_at": expires_at,
        "trusted_keys": trusted,
        "policy": {
            "require_signature": bool(require_signature),
            "min_release_seq": int(min_release_seq),
            "enforce_release_expiry": True,
            "allowed_mirrors": sorted({str(u).rstrip("/") + "/" for u in (mirror_urls or [])}),
        },
        "note": "Alpha11 local trust anchor. Distribute this file out-of-band; beta should add root signing/rotation/threshold policy.",
    }
    if expires_at:
        check = _time_check_window(expires_at=expires_at)
        # Allow intentionally expired test roots only if caller really asked for it;
        # writing is permitted, verification will reject it.
        root_doc["root_time_status_at_creation"] = check
    out = Path(out_file)
    write_json(out, root_doc)
    return {"ok": True, "root_file": str(out), "root_id": root_id, "trusted_key_count": len(trusted), "expires_at": expires_at, "min_release_seq": int(min_release_seq)}


def _read_trusted_root(root_file: str | Path) -> Dict[str, Any]:
    doc = read_json_local(Path(root_file))
    if doc.get("schema") != "CoreLangDistribution/TrustedRoot":
        raise ValueError("not a CoreLangDistribution trusted-root JSON")
    if doc.get("version") not in ("2.0-alpha6", "2.0-alpha7", "2.0-alpha8", "2.0-alpha12", "2.0-alpha13", "2.0-alpha15", "2.0-alpha17", "2.0-alpha18", "2.0-alpha19", "2.0-alpha24"):
        raise ValueError(f"unsupported trusted-root version: {doc.get('version')}")
    return doc




def _normalize_base_url(value: str | Path) -> str:
    text = str(value)
    if is_url(text):
        return text.rstrip("/") + "/"
    return text


def validate_trusted_root_sources(trusted_root: str | Path, sources: Sequence[str | Path]) -> Dict[str, Any]:
    root = _read_trusted_root(trusted_root)
    allowed = set((root.get("policy", {}) or {}).get("allowed_mirrors", []) or [])
    checked = [_normalize_base_url(s) for s in sources if s]
    if not allowed:
        return {"ok": True, "checked_sources": checked, "allowed_mirrors": [], "enforced": False, "errors": []}
    errors = [s for s in checked if is_url(s) and s not in allowed]
    return {"ok": not errors, "checked_sources": checked, "allowed_mirrors": sorted(allowed), "enforced": True, "errors": [f"source/mirror not allowed by trusted root: {e}" for e in errors]}


def _public_from_root_key(key_doc: Dict[str, Any]):
    _Ed25519PrivateKey, Ed25519PublicKey, _serialization = _require_ed25519()
    if key_doc.get("algorithm") != "ed25519":
        raise ValueError(f"unsupported root key algorithm: {key_doc.get('algorithm')}")
    public_raw = _b64d(str(key_doc.get("public_key", "")))
    key_id = sha256_bytes(public_raw)[:16]
    if key_doc.get("key_id") and key_doc.get("key_id") != key_id:
        raise ValueError("trusted root key_id does not match public key bytes")
    if key_doc.get("public_key_sha256") and key_doc.get("public_key_sha256") != sha256_bytes(public_raw):
        raise ValueError("trusted root public_key_sha256 mismatch")
    return Ed25519PublicKey.from_public_bytes(public_raw), public_raw, key_id


def _verify_signature_with_key_doc(source: str | Path, key_doc: Dict[str, Any]) -> Dict[str, Any]:
    repo = load_repo(source)
    sig_name = repo.release.get("signatures", "signatures.json")
    try:
        sig_doc = _repo_json(repo, sig_name)
    except Exception as e:
        return {"ok": False, "error": f"signature metadata missing or unreadable: {e}"}
    if sig_doc.get("algorithm") != "ed25519":
        return {"ok": False, "error": f"unsupported signature algorithm: {sig_doc.get('algorithm')}"}
    if sig_doc.get("signed_root_hash") != repo.release.get("root_hash"):
        return {"ok": False, "error": "signature signed_root_hash does not match release root_hash"}
    if sig_doc.get("signed_expires_at") != repo.release.get("expires_at"):
        return {"ok": False, "error": "signature signed_expires_at does not match release expires_at"}
    if sig_doc.get("signed_not_before") != repo.release.get("not_before"):
        return {"ok": False, "error": "signature signed_not_before does not match release not_before"}
    try:
        public, public_raw, key_id = _public_from_root_key(key_doc)
        if sig_doc.get("key_id") != key_id:
            return {"ok": False, "algorithm": sig_doc.get("algorithm"), "key_id": sig_doc.get("key_id"), "trusted_key_id": key_id, "error": "signature key_id does not match trusted-root key"}
        if sig_doc.get("public_key_sha256") and sig_doc.get("public_key_sha256") != sha256_bytes(public_raw):
            return {"ok": False, "algorithm": sig_doc.get("algorithm"), "key_id": sig_doc.get("key_id"), "trusted_key_id": key_id, "error": "signature public_key_sha256 does not match trusted-root key"}
        public.verify(_b64d(str(sig_doc.get("signature", ""))), _signature_payload(repo))
    except Exception as e:
        return {"ok": False, "algorithm": sig_doc.get("algorithm"), "key_id": sig_doc.get("key_id"), "error": f"signature mismatch: {e}"}
    return {"ok": True, "algorithm": sig_doc.get("algorithm"), "key_id": sig_doc.get("key_id"), "signed_root_hash": sig_doc.get("signed_root_hash"), "error": None}


def verify_repo_trust(source: str | Path, trusted_root: str | Path, *, at_time: str | None = None) -> Dict[str, Any]:
    repo = load_repo(source)
    root = _read_trusted_root(trusted_root)
    errors: list[str] = []
    root_time = _time_check_window(expires_at=root.get("expires_at"), at_time=at_time)
    if not root_time.get("ok"):
        errors.append(f"trusted root {root_time.get('error')}")
    policy = root.get("policy", {}) or {}
    min_seq = int(policy.get("min_release_seq", 0))
    release_seq = int(repo.release.get("release_seq", 0))
    if release_seq < min_seq:
        errors.append(f"release_seq {release_seq} is below trusted-root min_release_seq {min_seq}")
    release_time = {"ok": True, "error": None}
    if policy.get("enforce_release_expiry", True):
        release_time = _release_time_status(repo, at_time=at_time)
        if not release_time.get("ok"):
            errors.append(f"release {release_time.get('error')}")
    sig_result = None
    sig_doc = None
    try:
        sig_doc = _repo_json(repo, repo.release.get("signatures", "signatures.json"))
    except Exception as e:
        if policy.get("require_signature", True):
            errors.append(f"signature metadata missing or unreadable: {e}")
    if sig_doc is not None:
        key_id = sig_doc.get("key_id")
        matching = [k for k in root.get("trusted_keys", []) if k.get("key_id") == key_id]
        if not matching:
            errors.append(f"signature key_id {key_id} is not trusted by root")
        else:
            sig_result = _verify_signature_with_key_doc(source, matching[0])
            if not sig_result.get("ok"):
                errors.append(f"signature verification failed: {sig_result.get('error')}")
    return {
        "ok": not errors,
        "errors": errors,
        "root_id": root.get("root_id"),
        "root_file": str(trusted_root),
        "trusted_key_count": len(root.get("trusted_keys", [])),
        "policy": policy,
        "release_id": repo.release.get("release_id"),
        "release_seq": release_seq,
        "release_expires_at": repo.release.get("expires_at"),
        "root_time": root_time,
        "release_time": release_time,
        "signature": sig_result,
    }


def metadata_root_hash(files: List[Dict[str, Any]], chunks: List[Dict[str, Any]], packs: List[Dict[str, Any]], release_id: str, release_seq: int) -> str:
    payload = {
        "files_sha256": sha256_bytes(json_bytes(files)),
        "chunks_sha256": sha256_bytes(json_bytes(chunks)),
        "packs": packs,
        "release_id": release_id,
        "release_seq": release_seq,
    }
    return sha256_bytes(json_bytes(payload))


@dataclass
class LoadedRepo:
    source: str
    release: Dict[str, Any]
    files: List[Dict[str, Any]]
    chunks: Dict[str, Dict[str, Any]]
    mirrors: List[str] = field(default_factory=list)
    etag_cache: Dict[str, str] = field(default_factory=dict)
    network_stats: Dict[str, Any] = field(default_factory=new_network_stats)
    http_retries: int = 3
    http_backoff: float = 0.05
    mirror_policy: str = "ordered"
    hedge_delay: float = 0.0
    mirror_blacklist_threshold: int = 0
    mirror_blacklist_seconds: float = 0.0
    bandwidth_limit_bps: int = 0
    mirror_state_file: str | None = None
    token_bucket: TokenBucket = field(default_factory=TokenBucket)

    @property
    def is_remote(self) -> bool:
        return is_url(self.source)

    def pack_ref(self, pack_id: str) -> str | Path:
        rel = f"packs/{pack_id}"
        if self.is_remote:
            return url_join(self.source, rel)
        return Path(self.source) / rel

    def pack_candidates(self, pack_id: str) -> list[str | Path]:
        rel = f"packs/{pack_id}"
        if self.is_remote:
            # Alpha11: primary first, then mirrors, but temporarily skip bases that
            # crossed the per-fetch blacklist threshold. This is intentionally
            # in-memory only; persistent scoring is left for beta.
            bases = [self.source] + [m for m in self.mirrors if m]
            out: list[str] = []
            deferred: list[str] = []
            seen: set[str] = set()
            now = time.time()
            mirror_stats = self.network_stats.setdefault("mirrors", {})
            for base in bases:
                url = url_join(base, rel)
                if url in seen:
                    continue
                seen.add(url)
                key = _mirror_key(url)
                entry = mirror_stats.get(key, {})
                blacklisted_until = float(entry.get("blacklisted_until", 0) or 0)
                if blacklisted_until > now:
                    deferred.append(url)
                else:
                    out.append(url)
            # Keep blacklisted mirrors as a last resort so availability wins over scoring.
            return out + deferred
        return [Path(self.source) / rel]


def load_repo(
    source: str | Path,
    *,
    mirrors: Sequence[str] | None = None,
    http_retries: int = 3,
    http_backoff: float = 0.05,
    mirror_policy: str = "ordered",
    hedge_delay: float = 0.0,
    mirror_blacklist_threshold: int = 0,
    mirror_blacklist_seconds: float = 0.0,
    bandwidth_limit_bps: str | int = 0,
    mirror_state_file: str | Path | None = None,
) -> LoadedRepo:
    src = str(source)
    stats = new_network_stats()
    mirror_list = [str(m) for m in (mirrors or [])]
    if is_url(src):
        errors: list[str] = []
        release = files = chunks_list = None
        metadata_source = src
        for base in [src] + mirror_list:
            try:
                release = json.loads(read_url(url_join(base, "release.json"), retries=http_retries, backoff=http_backoff, stats=stats).decode("utf-8"))
                files = json.loads(read_url(url_join(base, release["files"]), retries=http_retries, backoff=http_backoff, stats=stats).decode("utf-8"))
                chunks_list = json.loads(read_url(url_join(base, release["chunks"]), retries=http_retries, backoff=http_backoff, stats=stats).decode("utf-8"))
                metadata_source = base
                if base != src:
                    stats["metadata_mirror_used"] = base
                break
            except Exception as e:
                errors.append(f"{base}: {e}")
                continue
        if release is None or files is None or chunks_list is None:
            raise IOError("metadata load failed from primary and mirrors: " + " | ".join(errors[:5]))
        src_for_repo = metadata_source
    else:
        root = Path(src)
        release = read_json_local(root / "release.json")
        files = read_json_local(root / release["files"])
        chunks_list = read_json_local(root / release["chunks"])
        src_for_repo = src
    chunks = {c["chunk_id"]: c for c in chunks_list}
    repo = LoadedRepo(src_for_repo, release, files, chunks)
    repo.mirrors = mirror_list
    repo.http_retries = int(http_retries)
    repo.http_backoff = float(http_backoff)
    repo.mirror_policy = mirror_policy
    repo.hedge_delay = float(hedge_delay or 0.0)
    repo.mirror_blacklist_threshold = int(mirror_blacklist_threshold or 0)
    repo.mirror_blacklist_seconds = float(mirror_blacklist_seconds or 0.0)
    repo.bandwidth_limit_bps = parse_size(bandwidth_limit_bps) if bandwidth_limit_bps else 0
    repo.token_bucket = TokenBucket(repo.bandwidth_limit_bps)
    repo.mirror_state_file = str(mirror_state_file) if mirror_state_file else None
    repo.network_stats = stats
    repo.network_stats["mirror_policy"] = mirror_policy
    repo.network_stats["bandwidth_limit_bps"] = repo.bandwidth_limit_bps
    repo.network_stats["token_bucket"] = repo.token_bucket.snapshot()
    if repo.mirror_state_file:
        _load_mirror_state_into(repo, repo.mirror_state_file)
    return repo


def make_repo(
    input_dir: str | Path,
    out_repo: str | Path,
    *,
    release_id: str = "dev",
    release_seq: int | None = None,
    chunker: str = "cdc",
    fixed_size: str | int = "256KiB",
    chunk_min: str | int = "64KiB",
    chunk_avg: str | int = "256KiB",
    chunk_max: str | int = "1MiB",
    fastcdc_stride: int = 16,
    codec: str = "auto",
    force: bool = False,
    expires_at: str | None = None,
    not_before: str | None = None,
    profile_metadata: dict[str, Any] | None = None,
) -> Dict[str, Any]:
    root = Path(input_dir).resolve()
    out = Path(out_repo).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"input directory not found: {root}")
    if out.exists():
        if force:
            shutil.rmtree(out)
        else:
            raise FileExistsError(f"output repo already exists: {out}")
    (out / "packs").mkdir(parents=True)

    release_seq_i = int(release_seq or 0)
    fixed_b = parse_size(fixed_size)
    min_b = parse_size(chunk_min)
    avg_b = parse_size(chunk_avg)
    max_b = parse_size(chunk_max)

    pack_id = "pack-000000.cldpak"
    pack_path = out / "packs" / pack_id
    files: List[Dict[str, Any]] = []
    chunks: Dict[str, Dict[str, Any]] = {}
    logical_size = 0
    stored_bytes = 0
    reused_within_repo = 0
    chunk_lengths: List[int] = []

    with pack_path.open("wb") as pack:
        for p in sorted(x for x in root.rglob("*") if x.is_file()):
            # Do not package CLD2 internal metadata/cache accidentally.
            if ".cld2" in p.parts:
                continue
            rel = safe_rel(p, root)
            file_hasher = hashlib.sha256()
            file_size = 0
            file_chunks: List[Dict[str, Any]] = []
            # Alpha18: stream chunks from disk instead of p.read_bytes(). This keeps
            # large-file packaging memory-bounded and enables a faster fastcdc mode.
            for off, raw in iter_file_chunks(p, chunker, fixed_b, min_b, avg_b, max_b, fastcdc_stride=fastcdc_stride):
                file_hasher.update(raw)
                file_size += len(raw)
                cid = sha256_bytes(raw)
                chunk_lengths.append(len(raw))
                if cid in chunks:
                    reused_within_repo += 1
                    centry = chunks[cid]
                else:
                    packed = compress(raw, codec=codec, path_hint=rel)
                    comp_hash = sha256_bytes(packed.payload)
                    pack_offset = pack.tell()
                    pack.write(packed.payload)
                    stored_bytes += len(packed.payload)
                    centry = {
                        "chunk_id": cid,
                        "raw_hash": cid,
                        "compressed_hash": comp_hash,
                        "raw_len": len(raw),
                        "compressed_len": len(packed.payload),
                        "codec": packed.codec,
                        "pack_id": pack_id,
                        "pack_offset": pack_offset,
                        "pack_len": len(packed.payload),
                    }
                    chunks[cid] = centry
                file_chunks.append({"chunk_id": cid, "file_offset": off, "raw_len": len(raw)})
            file_hash = file_hasher.hexdigest()
            logical_size += file_size
            st = p.stat()
            mode = int(st.st_mode) & 0o777
            files.append({
                "path": rel,
                "size": file_size,
                "file_hash": file_hash,
                "mode": mode,
                "mtime_ns": int(st.st_mtime_ns),
                "executable": bool(mode & 0o111),
                "chunks": file_chunks,
            })

    chunk_list = sorted(chunks.values(), key=lambda c: (c["pack_id"], c["pack_offset"]))
    packs = [{"pack_id": pack_id, "size": pack_path.stat().st_size, "hash": sha256_file(pack_path)}]
    root_hash = metadata_root_hash(files, chunk_list, packs, release_id, release_seq_i)
    release = {
        "schema": "CoreLangDistribution/Release",
        "version": "2.0-alpha24",
        "tool": f"CoreLangDistribution {__version__}",
        "release_id": release_id,
        "release_seq": release_seq_i,
        "root_hash": root_hash,
        "created_at": now_iso(),
        "expires_at": expires_at,
        "not_before": not_before,
        "files": "files.idx.json",
        "chunks": "chunks.idx.json",
        "packs": packs,
        "chunker": {"mode": chunker, "fixed_size": fixed_b, "min": min_b, "avg": avg_b, "max": max_b, "fastcdc_stride": int(fastcdc_stride)},
        "codec_policy": codec,
        "metrics": {
            "logical_size": logical_size,
            "stored_bytes": stored_bytes,
            "file_count": len(files),
            "unique_chunk_count": len(chunk_list),
            "reused_chunks_within_repo": reused_within_repo,
            "chunk_size_stats": chunk_stats(chunk_lengths),
            "storage_ratio": round(stored_bytes / logical_size, 6) if logical_size else 0,
        },
    }
    if profile_metadata:
        release["profile"] = dict(profile_metadata)
    write_json(out / "files.idx.json", files)
    write_json(out / "chunks.idx.json", chunk_list)
    write_json(out / "release.json", release)
    return release


def inspect_repo(source: str | Path) -> Dict[str, Any]:
    repo = load_repo(source)
    metrics = dict(repo.release.get("metrics", {}))
    metrics.update({
        "release_id": repo.release.get("release_id"),
        "release_seq": repo.release.get("release_seq", 0),
        "root_hash": repo.release.get("root_hash"),
        "version": repo.release.get("version"),
        "file_count": len(repo.files),
        "unique_chunk_count": len(repo.chunks),
        "source": str(source),
    })
    return metrics


def _pack_etag(repo: LoadedRepo, pack_url: str) -> str | None:
    if pack_url in repo.etag_cache:
        return repo.etag_cache[pack_url]
    try:
        headers = head_url(pack_url, retries=repo.http_retries, backoff=repo.http_backoff, stats=repo.network_stats)
        etag = headers.get("etag")
        if etag:
            repo.etag_cache[pack_url] = etag
            return etag
    except Exception:
        # ETag is an optimization/safety feature for remote ranges. If HEAD is
        # unavailable, alpha17 falls back to Range without If-Range and still
        # verifies every chunk by hash.
        return None
    return None


def _mirror_key(pack_ref: str | Path) -> str:
    if isinstance(pack_ref, Path):
        return "local"
    # Pack URL includes /packs/<file>; trim that suffix to report the serving mirror/base.
    text = str(pack_ref)
    needle = "/packs/"
    return text.split(needle, 1)[0] + "/" if needle in text else text




def _load_mirror_state_into(repo: LoadedRepo, state_file: str | Path) -> None:
    path = Path(state_file)
    if not path.exists():
        repo.network_stats["mirror_state_loaded"] = False
        repo.network_stats["mirror_state_file"] = str(path)
        return
    try:
        doc = read_json_local(path)
        mirrors = doc.get("mirrors", {}) if isinstance(doc, dict) else {}
        if isinstance(mirrors, dict):
            repo.network_stats.setdefault("mirrors", {}).update(mirrors)
            repo.network_stats["mirror_state_loaded"] = True
            repo.network_stats["mirror_state_entries"] = len(mirrors)
        else:
            repo.network_stats["mirror_state_loaded"] = False
    except Exception as e:
        repo.network_stats["mirror_state_loaded"] = False
        repo.network_stats["mirror_state_error"] = str(e)[:240]
    repo.network_stats["mirror_state_file"] = str(path)


def _save_mirror_state(repo: LoadedRepo) -> None:
    if not repo.mirror_state_file:
        return
    path = Path(repo.mirror_state_file)
    mirrors = repo.network_stats.get("mirrors", {})
    serializable = {}
    for key, value in mirrors.items():
        serializable[key] = {
            "attempts": int(value.get("attempts", 0)),
            "successes": int(value.get("successes", 0)),
            "failures": int(value.get("failures", 0)),
            "blacklist_events": int(value.get("blacklist_events", 0)),
            "score": int(value.get("score", 0)),
            "last_error": value.get("last_error"),
            "updated_at": now_iso(),
        }
    write_json(path, {
        "schema": "CoreLangDistribution/MirrorState",
        "version": "2.0-alpha24",
        "updated_at": now_iso(),
        "mirrors": serializable,
    })
    repo.network_stats["mirror_state_saved"] = True
    repo.network_stats["mirror_state_file"] = str(path)


def _mirror_stats(repo: LoadedRepo, pack_ref: str | Path) -> dict[str, Any]:
    stats = repo.network_stats.setdefault("mirrors", {})
    key = _mirror_key(pack_ref)
    entry = stats.setdefault(key, {
        "attempts": 0,
        "successes": 0,
        "failures": 0,
        "bytes_requested": 0,
        "last_error": None,
        "hedged_attempts": 0,
        "blacklisted_until": 0.0,
        "blacklist_events": 0,
        "score": 0,
    })
    return entry


def _read_and_verify_from_pack_ref(repo: LoadedRepo, pack_ref: str | Path, centry: Dict[str, Any], *, hedged: bool = False) -> bytes:
    entry = _mirror_stats(repo, pack_ref)
    entry["attempts"] += 1
    entry["bytes_requested"] += int(centry["pack_len"])
    if hedged:
        entry["hedged_attempts"] += 1
    try:
        if isinstance(pack_ref, Path):
            data = read_local_range(pack_ref, centry["pack_offset"], centry["pack_len"])
        else:
            etag = _pack_etag(repo, str(pack_ref))
            data = read_url_range(
                str(pack_ref),
                centry["pack_offset"],
                centry["pack_len"],
                etag=etag,
                retries=repo.http_retries,
                backoff=repo.http_backoff,
                stats=repo.network_stats,
            )
        if repo.bandwidth_limit_bps and not isinstance(pack_ref, Path):
            before = repo.token_bucket.snapshot()
            repo.token_bucket.consume(len(data))
            after = repo.token_bucket.snapshot()
            repo.network_stats["throttle_sleep_seconds"] = after["sleep_seconds"]
            repo.network_stats["throttle_events"] = after["events"]
            repo.network_stats["token_bucket"] = after
        if sha256_bytes(data) != centry["compressed_hash"]:
            raise IOError(f"compressed hash mismatch for chunk {centry['chunk_id']}")
        raw = decompress(centry["codec"], data, raw_len=centry["raw_len"])
        if sha256_bytes(raw) != centry["raw_hash"]:
            raise IOError(f"raw hash mismatch for chunk {centry['chunk_id']}")
        entry["successes"] += 1
        entry["score"] = int(entry.get("successes", 0)) - int(entry.get("failures", 0))
        return raw
    except Exception as e:
        entry["failures"] += 1
        entry["score"] = int(entry.get("successes", 0)) - int(entry.get("failures", 0))
        entry["last_error"] = str(e)[:240]
        if repo.mirror_blacklist_threshold and int(entry.get("failures", 0)) >= repo.mirror_blacklist_threshold:
            entry["blacklisted_until"] = time.time() + max(0.0, repo.mirror_blacklist_seconds)
            entry["blacklist_events"] = int(entry.get("blacklist_events", 0)) + 1
            repo.network_stats["mirror_blacklist_events"] = int(repo.network_stats.get("mirror_blacklist_events", 0)) + 1
        raise


def _read_packed_chunk_ordered(repo: LoadedRepo, centry: Dict[str, Any]) -> bytes:
    errors: list[str] = []
    candidates = repo.pack_candidates(centry["pack_id"])
    for pack_ref in candidates:
        try:
            return _read_and_verify_from_pack_ref(repo, pack_ref, centry)
        except Exception as e:
            errors.append(f"{_mirror_key(pack_ref)}: {e}")
            continue
    raise IOError(f"all mirrors failed for chunk {centry['chunk_id']}: " + " | ".join(errors[:5]))


def _read_packed_chunk_hedged(repo: LoadedRepo, centry: Dict[str, Any]) -> bytes:
    candidates = repo.pack_candidates(centry["pack_id"])
    if len(candidates) <= 1 or repo.hedge_delay <= 0:
        return _read_packed_chunk_ordered(repo, centry)
    repo.network_stats["hedged_requests"] = int(repo.network_stats.get("hedged_requests", 0)) + 1
    # Alpha11 hedging is deliberately conservative: race the primary and the first mirror only.
    chosen = candidates[:2]
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
        futures = []
        futures.append(pool.submit(_read_and_verify_from_pack_ref, repo, chosen[0], centry, hedged=True))
        time.sleep(max(0.0, repo.hedge_delay))
        futures.append(pool.submit(_read_and_verify_from_pack_ref, repo, chosen[1], centry, hedged=True))
        pending = set(futures)
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                try:
                    raw = fut.result()
                    for pnd in pending:
                        pnd.cancel()
                    repo.network_stats["hedged_wins"] = int(repo.network_stats.get("hedged_wins", 0)) + 1
                    return raw
                except Exception as e:
                    errors.append(str(e))
                    continue
    # If both raced candidates failed, continue ordered through all mirrors, including any not raced.
    try:
        return _read_packed_chunk_ordered(repo, centry)
    except Exception as e:
        errors.append(str(e))
        raise IOError(f"hedged mirror read failed for chunk {centry['chunk_id']}: " + " | ".join(errors[:5])) from e


def _read_packed_chunk(repo: LoadedRepo, centry: Dict[str, Any]) -> bytes:
    if repo.is_remote and repo.mirrors and repo.mirror_policy == "hedged":
        return _read_packed_chunk_hedged(repo, centry)
    return _read_packed_chunk_ordered(repo, centry)


def extract_file(
    source: str | Path,
    member_path: str,
    out_file: str | Path,
    *,
    verify: bool = True,
    mirrors: Sequence[str] | None = None,
    mirror_policy: str = "ordered",
    hedge_delay: float = 0.0,
    http_retries: int = 3,
    http_backoff: float = 0.05,
    mirror_blacklist_threshold: int = 0,
    mirror_blacklist_seconds: float = 0.0,
    bandwidth_limit_bps: str | int = 0,
) -> Dict[str, Any]:
    ensure_safe_member(member_path)
    repo = load_repo(source, mirrors=mirrors, mirror_policy=mirror_policy, hedge_delay=hedge_delay, http_retries=http_retries, http_backoff=http_backoff, mirror_blacklist_threshold=mirror_blacklist_threshold, mirror_blacklist_seconds=mirror_blacklist_seconds, bandwidth_limit_bps=bandwidth_limit_bps)
    candidates = [f for f in repo.files if f["path"] == member_path]
    if not candidates:
        raise FileNotFoundError(f"path not found in repo: {member_path}")
    fentry = candidates[0]
    out = Path(out_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out.open("wb") as dst:
        for fc in fentry["chunks"]:
            centry = repo.chunks[fc["chunk_id"]]
            raw = _read_packed_chunk(repo, centry)
            dst.write(raw)
            written += len(raw)
    _apply_file_metadata(out, fentry)
    ok = True
    if verify:
        actual = sha256_bytes(out.read_bytes())
        ok = actual == fentry["file_hash"] and out.stat().st_size == fentry["size"]
        if not ok:
            raise IOError(f"file verification failed: {member_path}")
    if repo.is_remote:
        _save_mirror_state(repo)
    return {"path": member_path, "out_file": str(out), "bytes": written, "verified": ok, "network": repo.network_stats if repo.is_remote else None}


def verify_repo(source: str | Path, *, deep: bool = False, trust_key: str | Path | None = None, trusted_root: str | Path | None = None) -> Dict[str, Any]:
    repo = load_repo(source)
    errors: List[str] = []
    chunk_ids = set(repo.chunks)
    for f in repo.files:
        try:
            ensure_safe_member(f["path"])
        except Exception as e:
            errors.append(str(e))
        if f["size"] == 0 and f["file_hash"] != EMPTY_SHA256:
            errors.append(f"empty file has wrong hash: {f['path']}")
        for fc in f["chunks"]:
            if fc["chunk_id"] not in chunk_ids:
                errors.append(f"missing chunk {fc['chunk_id']} for {f['path']}")
    for p in repo.release.get("packs", []):
        try:
            pack_ref = repo.pack_ref(p["pack_id"])
            if isinstance(pack_ref, Path):
                data = pack_ref.read_bytes()
            else:
                data = read_url(str(pack_ref))
            if sha256_bytes(data) != p["hash"]:
                errors.append(f"pack hash mismatch: {p['pack_id']}")
        except Exception as e:
            errors.append(f"pack read failed {p.get('pack_id')}: {e}")
    expected_root = repo.release.get("root_hash")
    if expected_root:
        packs = repo.release.get("packs", [])
        actual_root = metadata_root_hash(repo.files, list(repo.chunks.values()), packs, repo.release.get("release_id", ""), int(repo.release.get("release_seq", 0)))
        if actual_root != expected_root:
            errors.append("metadata root_hash mismatch")
    signature = None
    trust = None
    if trust_key is not None:
        signature = verify_repo_signature(source, trust_key)
        if not signature.get("ok"):
            errors.append(f"signature verification failed: {signature.get('error')}")
    if trusted_root is not None:
        trust = verify_repo_trust(source, trusted_root)
        if not trust.get("ok"):
            errors.extend([f"trusted-root verification failed: {e}" for e in trust.get("errors", [])])
    if deep:
        for _cid, centry in repo.chunks.items():
            try:
                _read_packed_chunk(repo, centry)
            except Exception as e:
                errors.append(str(e))
                break
    return {"ok": not errors, "errors": errors, "files": len(repo.files), "chunks": len(repo.chunks), "deep": deep, "signature": signature, "trusted_root": trust}


def release_check(source: str | Path, *, deep: bool = False, strict_portability: bool = True) -> Dict[str, Any]:
    """Run alpha24 release hygiene checks.

    This is intentionally broader than verify_repo(): verify answers "is it
    internally valid?", release-check answers "is it safe/portable enough to
    distribute to mixed Windows/macOS/Linux clients?".
    """
    repo = load_repo(source)
    errors: list[str] = []
    warnings: list[str] = []
    path_reports: list[dict[str, Any]] = []

    if repo.release.get("schema") != "CoreLangDistribution/Release":
        errors.append(f"unexpected release schema: {repo.release.get('schema')}")
    version = str(repo.release.get("version", ""))
    if not version.startswith("2.0-alpha"):
        warnings.append(f"unexpected/non-alpha release version: {version}")

    seen_exact: dict[str, str] = {}
    seen_casefold: dict[str, str] = {}
    seen_nfc_casefold: dict[str, str] = {}
    file_hashes: set[str] = set()
    for f in repo.files:
        path = str(f.get("path", ""))
        file_hashes.add(str(f.get("file_hash", "")))
        try:
            ensure_safe_member(path)
        except Exception as e:
            errors.append(f"{path}: {e}")
            continue
        issues = path_portability_issues(path)
        if issues["errors"] or issues["warnings"]:
            path_reports.append({"path": path, **issues})
        errors.extend(f"{path}: {e}" for e in issues["errors"])
        warnings.extend(f"{path}: {w}" for w in issues["warnings"])
        if path in seen_exact:
            errors.append(f"duplicate exact path: {path}")
        seen_exact[path] = path
        cf = _portable_path_key(path, casefold=True, unicode_form="NFC")
        if cf in seen_casefold and seen_casefold[cf] != path:
            errors.append(f"case/Unicode-insensitive path collision: {seen_casefold[cf]!r} vs {path!r}")
        else:
            seen_casefold[cf] = path
        nfc = _portable_path_key(path, casefold=False, unicode_form="NFC")
        if nfc in seen_nfc_casefold and seen_nfc_casefold[nfc] != path:
            warnings.append(f"Unicode NFC-normalization collision: {seen_nfc_casefold[nfc]!r} vs {path!r}")
        else:
            seen_nfc_casefold[nfc] = path
        if "mode" not in f:
            warnings.append(f"{path}: missing alpha24 mode metadata")
        if "mtime_ns" not in f:
            warnings.append(f"{path}: missing alpha24 mtime_ns metadata")

    # Chunk/pack sanity.
    pack_by_id = {p.get("pack_id"): p for p in repo.release.get("packs", [])}
    for cid, c in repo.chunks.items():
        if cid != c.get("raw_hash"):
            errors.append(f"chunk {cid}: raw_hash mismatch in metadata")
        if int(c.get("raw_len", -1)) < 0 or int(c.get("pack_len", -1)) < 0:
            errors.append(f"chunk {cid}: negative lengths")
        if c.get("pack_id") not in pack_by_id:
            errors.append(f"chunk {cid}: references missing pack {c.get('pack_id')}")
        else:
            pack_size = int(pack_by_id[c.get("pack_id")].get("size", 0))
            off = int(c.get("pack_offset", 0)); ln = int(c.get("pack_len", 0))
            if off < 0 or ln < 0 or off + ln > pack_size:
                errors.append(f"chunk {cid}: pack range outside {c.get('pack_id')}")

    # Reuse existing verifier for root/packs/chunks.
    verify = verify_repo(source, deep=deep)
    if not verify.get("ok"):
        errors.extend(f"verify: {e}" for e in verify.get("errors", []))

    portable_ok = not [e for e in errors if "path" in e or "Windows" in e or "collision" in e or "unsafe" in e]
    return {
        "ok": not errors,
        "portable_ok": portable_ok,
        "strict_portability": bool(strict_portability),
        "errors": errors,
        "warnings": warnings,
        "path_reports": path_reports[:200],
        "release_id": repo.release.get("release_id"),
        "release_seq": int(repo.release.get("release_seq", 0)),
        "version": repo.release.get("version"),
        "files": len(repo.files),
        "unique_file_hashes": len(file_hashes),
        "chunks": len(repo.chunks),
        "packs": len(repo.release.get("packs", [])),
        "deep": deep,
        "verify": verify,
    }


def create_patch_plan(old_source: str | Path, new_source: str | Path, out_file: str | Path | None = None) -> Dict[str, Any]:
    old = load_repo(old_source)
    new = load_repo(new_source)
    diff = diff_repos(old_source, new_source)
    added = sorted(set(new.chunks) - set(old.chunks))
    plan = {
        "schema": "CoreLangDistribution/PatchPlan",
        "version": "2.0-alpha24",
        "created_at": now_iso(),
        "old": {
            "release_id": old.release.get("release_id"),
            "release_seq": int(old.release.get("release_seq", 0)),
            "root_hash": old.release.get("root_hash"),
        },
        "new": {
            "release_id": new.release.get("release_id"),
            "release_seq": int(new.release.get("release_seq", 0)),
            "root_hash": new.release.get("root_hash"),
        },
        "download_required_pack_bytes": diff["download_required_pack_bytes"],
        "download_required_raw_bytes": diff["download_required_raw_bytes"],
        "chunk_count": len(added),
        "chunks": [
            {k: new.chunks[c][k] for k in ("chunk_id", "raw_hash", "compressed_hash", "raw_len", "compressed_len", "codec", "pack_id", "pack_offset", "pack_len")}
            for c in added
        ],
        "files": [{"path": f["path"], "size": f["size"], "file_hash": f["file_hash"]} for f in new.files],
        "diff_summary": diff,
    }
    if out_file:
        write_json(Path(out_file), plan)
    return plan


def validate_patch_plan(repo: LoadedRepo, patch_plan: str | Path, installed_manifest: Dict[str, Any] | None = None) -> Dict[str, Any]:
    plan = read_json_local(Path(patch_plan))
    if plan.get("schema") != "CoreLangDistribution/PatchPlan":
        raise ValueError("not a CoreLangDistribution patch plan")
    target = plan.get("new", {})
    old = plan.get("old", {})
    if target.get("root_hash") != repo.release.get("root_hash"):
        raise ValueError("patch plan target root_hash does not match repo")
    if target.get("release_id") != repo.release.get("release_id"):
        raise ValueError("patch plan target release_id does not match repo")
    if int(target.get("release_seq", -1)) != int(repo.release.get("release_seq", 0)):
        raise ValueError("patch plan target release_seq does not match repo")
    if int(target.get("release_seq", -1)) < int(old.get("release_seq", -1)):
        raise ValueError("patch plan target release_seq is older than source release_seq")
    if installed_manifest is None:
        raise ValueError("patch plan requires an existing installation matching the plan old release")
    if installed_manifest.get("root_hash") != old.get("root_hash"):
        raise ValueError("installed root_hash does not match patch plan old root_hash")
    if int(installed_manifest.get("release_seq", -1)) != int(old.get("release_seq", -1)):
        raise ValueError("installed release_seq does not match patch plan old release_seq")
    if installed_manifest.get("release_id") != old.get("release_id"):
        raise ValueError("installed release_id does not match patch plan old release_id")

    allowed: set[str] = set()
    for idx, pchunk in enumerate(plan.get("chunks", [])):
        cid = pchunk.get("chunk_id")
        if not cid or cid in allowed:
            raise ValueError(f"patch plan has missing/duplicate chunk_id at index {idx}")
        if cid not in repo.chunks:
            raise ValueError(f"patch plan references chunk not present in target repo: {cid}")
        rchunk = repo.chunks[cid]
        for k in ("raw_hash", "compressed_hash", "raw_len", "compressed_len", "codec", "pack_id", "pack_offset", "pack_len"):
            if pchunk.get(k) != rchunk.get(k):
                raise ValueError(f"patch plan chunk metadata mismatch for {cid}: {k}")
        allowed.add(cid)
    return {
        "plan": plan,
        "allowed_chunks": allowed,
        "planned_pack_bytes": int(plan.get("download_required_pack_bytes", 0)),
        "planned_chunks": len(allowed),
        "old_release_seq": int(old.get("release_seq", -1)),
        "new_release_seq": int(target.get("release_seq", -1)),
    }


def diff_repos(old_source: str | Path, new_source: str | Path) -> Dict[str, Any]:
    old = load_repo(old_source)
    new = load_repo(new_source)
    old_chunks = set(old.chunks)
    new_chunks = set(new.chunks)
    reused = old_chunks & new_chunks
    added = new_chunks - old_chunks
    removed = old_chunks - new_chunks

    old_files = {f["path"]: f for f in old.files}
    changed_files = []
    unchanged_files = []
    added_files = []
    removed_files = sorted(set(old_files) - {f["path"] for f in new.files})
    for f in new.files:
        old_f = old_files.get(f["path"])
        if old_f is None:
            added_files.append(f["path"])
            changed_files.append(f)
        elif old_f.get("file_hash") == f.get("file_hash"):
            unchanged_files.append(f["path"])
        else:
            changed_files.append(f)

    logical_new = sum(int(f["size"]) for f in new.files)
    logical_changed_files = sum(int(f["size"]) for f in changed_files)
    download_required = sum(int(new.chunks[c]["pack_len"]) for c in added)
    raw_download_required = sum(int(new.chunks[c]["raw_len"]) for c in added)
    stored_new = sum(int(c["pack_len"]) for c in new.chunks.values())
    raw_new_unique = sum(int(c["raw_len"]) for c in new.chunks.values())

    return {
        "old_release_id": old.release.get("release_id"),
        "old_release_seq": old.release.get("release_seq", 0),
        "new_release_id": new.release.get("release_id"),
        "new_release_seq": new.release.get("release_seq", 0),
        "logical_size_new": logical_new,
        "stored_size_new": stored_new,
        "raw_unique_size_new": raw_new_unique,
        "old_unique_chunks": len(old_chunks),
        "new_unique_chunks": len(new_chunks),
        "reused_chunks": len(reused),
        "added_chunks": len(added),
        "removed_chunks": len(removed),
        "chunk_reuse_ratio": round(len(reused) / len(new_chunks), 6) if new_chunks else 1,
        "download_required_pack_bytes": download_required,
        "download_required_raw_bytes": raw_download_required,
        "estimated_saved_vs_full_stored_bytes": max(0, stored_new - download_required),
        "estimated_saved_ratio_vs_full_stored": round(max(0, stored_new - download_required) / stored_new, 6) if stored_new else 0,
        "file_level_changed_files": len(changed_files),
        "file_level_unchanged_files": len(unchanged_files),
        "file_level_added_files": len(added_files),
        "file_level_removed_files": len(removed_files),
        "file_level_raw_download_bytes": logical_changed_files,
        "estimated_saved_vs_file_level_raw_bytes": max(0, logical_changed_files - download_required),
        "estimated_saved_ratio_vs_file_level_raw": round(max(0, logical_changed_files - download_required) / logical_changed_files, 6) if logical_changed_files else 1,
        "estimated_raw_chunk_saved_vs_file_level_raw_bytes": max(0, logical_changed_files - raw_download_required),
        "estimated_raw_chunk_saved_ratio_vs_file_level_raw": round(max(0, logical_changed_files - raw_download_required) / logical_changed_files, 6) if logical_changed_files else 1,
        "changed_paths_sample": [f["path"] for f in changed_files[:20]],
        "removed_paths_sample": removed_files[:20],
    }


def _repo_chunker_params(repo: LoadedRepo) -> dict:
    cfg = repo.release.get("chunker", {})
    return {
        "mode": cfg.get("mode", "cdc"),
        "fixed_size": int(cfg.get("fixed_size", 256 * 1024)),
        "min_size": int(cfg.get("min", 64 * 1024)),
        "avg_size": int(cfg.get("avg", 256 * 1024)),
        "max_size": int(cfg.get("max", 1024 * 1024)),
    }


def seed_cache_from_installed(repo: LoadedRepo, installed_dir: str | Path, cache: Path) -> Dict[str, Any]:
    root = Path(installed_dir)
    if not root.exists():
        return {"seeded_chunks": 0, "seeded_raw_bytes": 0, "scanned_files": 0}
    required = set(repo.chunks)
    params = _repo_chunker_params(repo)
    seeded_chunks = 0
    seeded_raw_bytes = 0
    scanned_files = 0
    cache.mkdir(parents=True, exist_ok=True)
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        try:
            if ".cld2" in p.parts:
                continue
            scanned_files += 1
            for _off, raw in iter_file_chunks(p, params["mode"], params["fixed_size"], params["min_size"], params["avg_size"], params["max_size"]):
                cid = sha256_bytes(raw)
                if cid in required:
                    cp = cache / cid
                    if not cp.exists():
                        cp.write_bytes(raw)
                        seeded_chunks += 1
                        seeded_raw_bytes += len(raw)
        except Exception:
            continue
    return {"seeded_chunks": seeded_chunks, "seeded_raw_bytes": seeded_raw_bytes, "scanned_files": scanned_files}




# ---- Alpha11 cache/audit/repair helpers -------------------------------------------------

CACHE_INDEX_NAME = ".cld2_cache_index.json"

INSTALL_JOURNAL_SUFFIX = ".cld2-journal.json"
INSTALL_LOCK_SUFFIX = ".cld2.lock"


def _install_parent(install_dir: str | Path) -> Path:
    install = Path(install_dir)
    parent = install.parent if install.parent.exists() else Path.cwd()
    parent.mkdir(parents=True, exist_ok=True)
    return parent


def _install_journal_path(install_dir: str | Path) -> Path:
    install = Path(install_dir)
    return _install_parent(install) / f".{install.name}{INSTALL_JOURNAL_SUFFIX}"


def _install_lock_path(install_dir: str | Path) -> Path:
    install = Path(install_dir)
    return _install_parent(install) / f".{install.name}{INSTALL_LOCK_SUFFIX}"


def _staging_glob(install_dir: str | Path) -> list[Path]:
    install = Path(install_dir)
    parent = _install_parent(install)
    return sorted(parent.glob(f".{install.name}.cld2-staging-*"))


def _backup_path(install_dir: str | Path) -> Path:
    install = Path(install_dir)
    return install.with_name(install.name + ".bak-cld2")


def _path_age_seconds(path: Path) -> float | None:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except Exception:
        return None


def _write_install_journal(install_dir: str | Path, **fields: Any) -> Path:
    path = _install_journal_path(install_dir)
    current: dict[str, Any] = {}
    if path.exists():
        try:
            current = read_json_local(path)
        except Exception:
            current = {}
    current.update(fields)
    current.setdefault("schema", "CoreLangDistribution/InstallJournal")
    current.setdefault("version", "2.0-alpha24")
    current["updated_at"] = now_iso()
    write_json(path, current)
    return path


def _clear_install_journal(install_dir: str | Path) -> None:
    try:
        _install_journal_path(install_dir).unlink(missing_ok=True)
    except Exception:
        pass


def _acquire_install_lock(install_dir: str | Path, *, stale_lock_seconds: float = 3600.0) -> Path:
    lock = _install_lock_path(install_dir)
    now = time.time()
    if lock.exists():
        age = _path_age_seconds(lock)
        if stale_lock_seconds is not None and stale_lock_seconds >= 0 and age is not None and age > stale_lock_seconds:
            try:
                lock.unlink()
            except Exception:
                pass
        if lock.exists():
            raise RuntimeError(f"install is locked by {lock}; use doctor --fix only if no cld2 process is running")
    doc = {
        "schema": "CoreLangDistribution/InstallLock",
        "version": "2.0-alpha24",
        "pid": os.getpid(),
        "created_at": now_iso(),
        "install_dir": str(Path(install_dir)),
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(str(lock), flags, 0o600)
    try:
        os.write(fd, json_bytes(doc))
    finally:
        os.close(fd)
    return lock


def _release_install_lock(lock: Path | None) -> None:
    if lock is None:
        return
    try:
        lock.unlink(missing_ok=True)
    except Exception:
        pass


def doctor_install(
    install_dir: str | Path,
    *,
    repo_source: str | Path | None = None,
    cache_dir: str | Path | None = None,
    fix: bool = False,
    stale_seconds: float = 0.0,
) -> Dict[str, Any]:
    """Inspect and optionally clean crash leftovers around an install directory.

    Alpha15 keeps this conservative: it removes staging dirs, stale locks and
    backup dirs only when --fix is requested, and it never deletes an existing
    install directory.
    """
    install = Path(install_dir)
    journal_path = _install_journal_path(install)
    lock_path = _install_lock_path(install)
    backup = _backup_path(install)
    staging_dirs = _staging_glob(install)
    stale_threshold = max(0.0, float(stale_seconds or 0.0))

    def eligible(path: Path) -> bool:
        age = _path_age_seconds(path)
        return age is not None and age >= stale_threshold

    journal = None
    if journal_path.exists():
        try:
            journal = read_json_local(journal_path)
        except Exception as e:
            journal = {"unreadable": str(e)}
    lock_doc = None
    if lock_path.exists():
        try:
            lock_doc = read_json_local(lock_path)
        except Exception as e:
            lock_doc = {"unreadable": str(e)}

    actions: list[dict[str, Any]] = []
    if fix:
        for sd in staging_dirs:
            if eligible(sd):
                shutil.rmtree(sd, ignore_errors=True)
                actions.append({"action": "removed-staging", "path": str(sd)})
        if backup.exists() and install.exists() and eligible(backup):
            shutil.rmtree(backup, ignore_errors=True)
            actions.append({"action": "removed-backup", "path": str(backup)})
        if lock_path.exists() and eligible(lock_path):
            lock_path.unlink(missing_ok=True)
            actions.append({"action": "removed-lock", "path": str(lock_path)})
        # Parent journal is only a diagnostic breadcrumb; completed installs have
        # their durable last_journal inside .cld2.
        if journal_path.exists() and eligible(journal_path):
            journal_path.unlink(missing_ok=True)
            actions.append({"action": "removed-journal", "path": str(journal_path)})

    audit = None
    if repo_source is not None:
        try:
            audit = audit_install(repo_source, install)
        except Exception as e:
            audit = {"ok": False, "error": str(e)}
    cache = None
    if cache_dir is not None:
        try:
            cache = rebuild_cache_index(cache_dir, prune_bad=fix)
        except Exception as e:
            cache = {"ok": False, "error": str(e)}

    # Refresh after optional fixes.
    remaining_staging = _staging_glob(install)
    lock_exists = lock_path.exists()
    backup_exists = backup.exists()
    journal_exists = journal_path.exists()
    ok = not remaining_staging and not lock_exists and not (backup_exists and install.exists()) and (audit is None or audit.get("ok") is True)
    return {
        "ok": ok,
        "install_dir": str(install),
        "fix": fix,
        "stale_seconds": stale_threshold,
        "journal": {"path": str(journal_path), "exists": journal_exists, "doc": journal},
        "lock": {"path": str(lock_path), "exists": lock_exists, "doc": lock_doc, "age_seconds": _path_age_seconds(lock_path) if lock_path.exists() else None},
        "backup": {"path": str(backup), "exists": backup_exists, "age_seconds": _path_age_seconds(backup) if backup.exists() else None},
        "staging": [{"path": str(p), "age_seconds": _path_age_seconds(p)} for p in remaining_staging],
        "audit": audit,
        "cache": cache,
        "actions": actions,
    }



def _cache_index_path(cache_dir: str | Path) -> Path:
    return Path(cache_dir) / CACHE_INDEX_NAME


def _is_chunk_filename(name: str) -> bool:
    return len(name) == 64 and all(c in "0123456789abcdef" for c in name.lower())


def read_cache_index(cache_dir: str | Path) -> Dict[str, Any] | None:
    path = _cache_index_path(cache_dir)
    if not path.exists():
        return None
    try:
        return read_json_local(path)
    except Exception:
        return None


def rebuild_cache_index(cache_dir: str | Path, *, prune_bad: bool = False) -> Dict[str, Any]:
    """Scan a raw-chunk cache and write a small index.

    The cache remains deliberately simple: each chunk is stored as a file named by
    its raw SHA-256. Alpha11 adds an index so future tools can audit/GC without
    trusting stale filenames blindly.
    """
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    entries: Dict[str, Any] = {}
    bad: list[dict[str, Any]] = []
    total_bytes = 0
    for p in sorted(cache.iterdir()):
        if not p.is_file() or p.name == CACHE_INDEX_NAME or p.name.endswith(".tmp"):
            continue
        if not _is_chunk_filename(p.name):
            bad.append({"path": str(p), "reason": "not-a-chunk-name"})
            if prune_bad:
                p.unlink(missing_ok=True)
            continue
        try:
            data = p.read_bytes()
            actual = sha256_bytes(data)
            if actual != p.name:
                bad.append({"path": str(p), "chunk_id": p.name, "actual_sha256": actual, "reason": "sha256-mismatch"})
                if prune_bad:
                    p.unlink(missing_ok=True)
                continue
            st = p.stat()
            entries[p.name] = {"raw_len": len(data), "mtime": int(st.st_mtime), "last_used": int(st.st_mtime)}
            total_bytes += len(data)
        except Exception as e:
            bad.append({"path": str(p), "reason": f"unreadable: {e}"})
    doc = {
        "schema": "CoreLangDistribution/CacheIndex",
        "version": "2.0-alpha24",
        "cache_dir": str(cache),
        "rebuilt_at": now_iso(),
        "chunk_count": len(entries),
        "total_raw_bytes": total_bytes,
        "bad_entries": bad,
        "chunks": entries,
    }
    write_json(_cache_index_path(cache), doc)
    return {"ok": True, "cache_dir": str(cache), "index_file": str(_cache_index_path(cache)), "chunk_count": len(entries), "total_raw_bytes": total_bytes, "bad_entries": len(bad), "bad_entry_details": bad[:20], "prune_bad": prune_bad}


def _cache_touch(cache: Path, cid: str, raw_len: int | None = None) -> None:
    """Best-effort update of the alpha24 cache index."""
    try:
        path = _cache_index_path(cache)
        doc = read_cache_index(cache) or {
            "schema": "CoreLangDistribution/CacheIndex",
            "version": "2.0-alpha24",
            "cache_dir": str(cache),
            "rebuilt_at": None,
            "chunk_count": 0,
            "total_raw_bytes": 0,
            "bad_entries": [],
            "chunks": {},
        }
        chunks = doc.setdefault("chunks", {})
        now_i = int(time.time())
        prev = chunks.get(cid, {})
        if raw_len is None:
            cp = cache / cid
            raw_len = cp.stat().st_size if cp.exists() else int(prev.get("raw_len", 0) or 0)
        chunks[cid] = {"raw_len": int(raw_len or 0), "mtime": int((cache / cid).stat().st_mtime) if (cache / cid).exists() else now_i, "last_used": now_i}
        doc["chunk_count"] = len(chunks)
        doc["total_raw_bytes"] = sum(int(v.get("raw_len", 0) or 0) for v in chunks.values())
        doc["updated_at"] = now_iso()
        write_json(path, doc)
    except Exception:
        pass


def cache_gc(cache_dir: str | Path, *, max_bytes: str | int = 0, min_age_seconds: float = 0.0) -> Dict[str, Any]:
    """Prune a cache down to max_bytes using least-recently-used metadata."""
    cache = Path(cache_dir)
    before = rebuild_cache_index(cache, prune_bad=True)
    limit = parse_size(max_bytes) if max_bytes else 0
    if limit <= 0:
        return {"ok": True, "cache_dir": str(cache), "mode": "index-only", "before": before, "removed_chunks": 0, "removed_bytes": 0, "after": rebuild_cache_index(cache)}
    doc = read_cache_index(cache) or {"chunks": {}}
    chunks = doc.get("chunks", {}) or {}
    total = int(doc.get("total_raw_bytes", 0) or 0)
    now_i = int(time.time())
    removed_chunks = 0
    removed_bytes = 0
    # Remove oldest last_used first; respect min_age_seconds to avoid deleting freshly downloaded chunks unless required.
    candidates = sorted(chunks.items(), key=lambda kv: (int(kv[1].get("last_used", kv[1].get("mtime", 0)) or 0), kv[0]))
    for cid, meta in candidates:
        if total <= limit:
            break
        age = now_i - int(meta.get("last_used", meta.get("mtime", now_i)) or now_i)
        if min_age_seconds and age < min_age_seconds:
            continue
        cp = cache / cid
        size = int(meta.get("raw_len", 0) or 0)
        if cp.exists():
            try:
                size = cp.stat().st_size
                cp.unlink()
            except Exception:
                continue
        total -= size
        removed_chunks += 1
        removed_bytes += size
    after = rebuild_cache_index(cache)
    return {"ok": True, "cache_dir": str(cache), "max_bytes": limit, "removed_chunks": removed_chunks, "removed_bytes": removed_bytes, "before": before, "after": after}


def audit_install(repo_source: str | Path, install_dir: str | Path) -> Dict[str, Any]:
    """Compare an installation directory against a release manifest."""
    repo = load_repo(repo_source)
    install = Path(install_dir)
    missing: list[dict[str, Any]] = []
    corrupt: list[dict[str, Any]] = []
    ok_files = 0
    expected_paths = {f["path"] for f in repo.files}
    for f in repo.files:
        ensure_safe_member(f["path"])
        p = install / f["path"]
        if not p.exists():
            missing.append({"path": f["path"], "reason": "missing"})
            continue
        if not p.is_file():
            corrupt.append({"path": f["path"], "reason": "not-a-file"})
            continue
        size = p.stat().st_size
        if size != int(f["size"]):
            corrupt.append({"path": f["path"], "reason": "size-mismatch", "expected_size": f["size"], "actual_size": size})
            continue
        actual = sha256_bytes(p.read_bytes())
        if actual != f["file_hash"]:
            corrupt.append({"path": f["path"], "reason": "hash-mismatch", "expected_sha256": f["file_hash"], "actual_sha256": actual})
            continue
        ok_files += 1
    extra: list[str] = []
    if install.exists():
        for p in sorted(x for x in install.rglob("*") if x.is_file()):
            try:
                if ".cld2" in p.parts:
                    continue
                rel = p.relative_to(install).as_posix()
                if rel not in expected_paths:
                    extra.append(rel)
            except Exception:
                continue
    manifest = read_installed_manifest(install)
    manifest_ok = bool(manifest and manifest.get("root_hash") == repo.release.get("root_hash") and int(manifest.get("release_seq", -999999)) == int(repo.release.get("release_seq", 0)))
    return {
        "ok": not missing and not corrupt,
        "repo_release_id": repo.release.get("release_id"),
        "repo_release_seq": int(repo.release.get("release_seq", 0)),
        "install_dir": str(install),
        "files_expected": len(repo.files),
        "files_ok": ok_files,
        "missing_count": len(missing),
        "corrupt_count": len(corrupt),
        "extra_count": len(extra),
        "missing": missing[:100],
        "corrupt": corrupt[:100],
        "extra": extra[:100],
        "installed_manifest_present": manifest is not None,
        "installed_manifest_matches_release": manifest_ok,
    }


def repair_install(
    repo_source: str | Path,
    install_dir: str | Path,
    *,
    cache_dir: str | Path | None = None,
    trust_key: str | Path | None = None,
    trusted_root: str | Path | None = None,
    mirrors: Sequence[str] | None = None,
    mirror_policy: str = "ordered",
    hedge_delay: float = 0.0,
    http_retries: int = 3,
    http_backoff: float = 0.05,
    parallel: int = 1,
    mirror_state_file: str | Path | None = None,
    stale_lock_seconds: float = 3600.0,
) -> Dict[str, Any]:
    """Audit and then rebuild an installation atomically using the normal fetch path."""
    before = audit_install(repo_source, install_dir)
    fetch = fetch_install(
        repo_source,
        install_dir,
        cache_dir=cache_dir,
        from_installed=install_dir,
        verify=True,
        allow_downgrade=True,
        trust_key=trust_key,
        trusted_root=trusted_root,
        mirrors=mirrors,
        mirror_policy=mirror_policy,
        hedge_delay=hedge_delay,
        http_retries=http_retries,
        http_backoff=http_backoff,
        parallel=parallel,
        mirror_state_file=mirror_state_file,
        stale_lock_seconds=stale_lock_seconds,
    )
    after = audit_install(repo_source, install_dir)
    return {"ok": after.get("ok") is True, "before": before, "fetch": fetch, "after": after, "repaired": before.get("ok") is not True and after.get("ok") is True}


def read_installed_manifest(install_dir: str | Path) -> Dict[str, Any] | None:
    path = Path(install_dir) / ".cld2" / "installed.json"
    if not path.exists():
        return None
    try:
        return read_json_local(path)
    except Exception:
        return None


def _check_rollback(repo: LoadedRepo, install: Path, allow_downgrade: bool) -> Dict[str, Any]:
    current = read_installed_manifest(install)
    target_seq = int(repo.release.get("release_seq", 0))
    current_seq = int((current or {}).get("release_seq", -1))
    if current is not None and target_seq < current_seq and not allow_downgrade:
        raise RuntimeError(f"refusing rollback: installed release_seq={current_seq}, target release_seq={target_seq}; pass allow_downgrade to override")
    return {"current_release_seq": current_seq if current else None, "target_release_seq": target_seq, "checked": current is not None}


def fetch_install(
    repo_source: str | Path,
    install_dir: str | Path,
    *,
    cache_dir: str | Path | None = None,
    from_installed: str | Path | None = None,
    verify: bool = True,
    allow_downgrade: bool = False,
    fail_after_chunks: int | None = None,
    patch_plan: str | Path | None = None,
    trust_key: str | Path | None = None,
    trusted_root: str | Path | None = None,
    mirrors: Sequence[str] | None = None,
    mirror_policy: str = "ordered",
    hedge_delay: float = 0.0,
    http_retries: int = 3,
    http_backoff: float = 0.05,
    mirror_blacklist_threshold: int = 0,
    mirror_blacklist_seconds: float = 0.0,
    bandwidth_limit_bps: str | int = 0,
    parallel: int = 1,
    mirror_state_file: str | Path | None = None,
    stale_lock_seconds: float = 3600.0,
) -> Dict[str, Any]:
    """Install all files from a repo, fetching only missing chunks.

    Alpha11 adds parallel chunk prefetch, mirror blacklisting and optional bandwidth limiting.
    """
    lock_path: Path | None = None
    repo = load_repo(repo_source, mirrors=mirrors, mirror_policy=mirror_policy, hedge_delay=hedge_delay, http_retries=http_retries, http_backoff=http_backoff, mirror_blacklist_threshold=mirror_blacklist_threshold, mirror_blacklist_seconds=mirror_blacklist_seconds, bandwidth_limit_bps=bandwidth_limit_bps, mirror_state_file=mirror_state_file)
    if trust_key is not None:
        sig = verify_repo_signature(repo_source, trust_key)
        if not sig.get("ok"):
            raise RuntimeError(f"release signature verification failed: {sig.get('error')}")
    trust_report = None
    source_policy_report = None
    if trusted_root is not None:
        trust_report = verify_repo_trust(repo_source, trusted_root)
        if not trust_report.get("ok"):
            raise RuntimeError("trusted-root verification failed: " + "; ".join(trust_report.get("errors", [])))
        source_policy_report = validate_trusted_root_sources(trusted_root, [repo_source] + list(mirrors or []))
        if not source_policy_report.get("ok"):
            raise RuntimeError("trusted-root mirror policy failed: " + "; ".join(source_policy_report.get("errors", [])))
    install = Path(install_dir)
    lock_path = _acquire_install_lock(install, stale_lock_seconds=stale_lock_seconds)
    try:
        current_installed = read_installed_manifest(install)
        rollback_report = _check_rollback(repo, install, allow_downgrade=allow_downgrade)
        patch_report = None
        allowed_patch_chunks = None
        if patch_plan is not None:
            patch_report = validate_patch_plan(repo, patch_plan, installed_manifest=current_installed)
            allowed_patch_chunks = patch_report["allowed_chunks"]
    except Exception:
        _release_install_lock(lock_path)
        raise

    parent = install.parent if install.parent.exists() else Path.cwd()
    staging = Path(tempfile.mkdtemp(prefix=f".{install.name}.cld2-staging-", dir=str(parent)))
    cache = Path(cache_dir) if cache_dir else Path(tempfile.mkdtemp(prefix="cld2-cache-"))
    cache.mkdir(parents=True, exist_ok=True)
    temp_cache = cache_dir is None
    cache_index_before = None
    if cache_dir is not None:
        try:
            cache_index_before = rebuild_cache_index(cache)
        except Exception:
            cache_index_before = {"ok": False, "error": "cache index rebuild failed"}
    backup: Path | None = None
    _write_install_journal(
        install,
        status="started",
        phase="prepare",
        source=str(repo_source),
        target_install_dir=str(install),
        staging_dir=str(staging),
        cache_dir=str(cache),
        target_release_id=repo.release.get("release_id"),
        target_release_seq=int(repo.release.get("release_seq", 0)),
        target_root_hash=repo.release.get("root_hash"),
        lock=str(lock_path) if lock_path else None,
        started_at=now_iso(),
    )

    seed_report = {"seeded_chunks": 0, "seeded_raw_bytes": 0, "scanned_files": 0}
    if from_installed:
        seed_report = seed_cache_from_installed(repo, from_installed, cache)

    downloaded_pack_bytes = 0
    downloaded_chunks = 0
    reused_cache_raw_bytes = 0
    cache_hit_chunks = 0
    memory_cache: Dict[str, bytes] = {}
    interrupted_for_test = False
    try:
        def get_raw(cid: str) -> bytes:
            nonlocal downloaded_pack_bytes, downloaded_chunks, reused_cache_raw_bytes, cache_hit_chunks, interrupted_for_test
            if cid in memory_cache:
                return memory_cache[cid]
            cp = cache / cid
            if cp.exists():
                raw = cp.read_bytes()
                if sha256_bytes(raw) == cid:
                    reused_cache_raw_bytes += len(raw)
                    cache_hit_chunks += 1
                    memory_cache[cid] = raw
                    _cache_touch(cache, cid, len(raw))
                    return raw
                cp.unlink(missing_ok=True)
            if allowed_patch_chunks is not None and cid not in allowed_patch_chunks:
                raise RuntimeError(f"patch plan does not authorize downloading missing chunk {cid}; seed cache/from-installed is incomplete")
            centry = repo.chunks[cid]
            raw = _read_packed_chunk(repo, centry)
            downloaded_pack_bytes += int(centry["pack_len"])
            downloaded_chunks += 1
            memory_cache[cid] = raw
            tmp = cp.with_suffix(".tmp")
            tmp.write_bytes(raw)
            tmp.replace(cp)
            _cache_touch(cache, cid, len(raw))
            if fail_after_chunks is not None and downloaded_chunks >= fail_after_chunks:
                interrupted_for_test = True
                raise RuntimeError(f"simulated interruption after {downloaded_chunks} downloaded chunks")
            return raw

        # Write staging state early so interrupted updates leave an inspectable trace.
        meta = staging / ".cld2"
        meta.mkdir(parents=True, exist_ok=True)
        write_json(meta / "fetch_state.json", {
            "schema": "CoreLangDistribution/FetchState",
            "target_release_id": repo.release.get("release_id"),
            "target_release_seq": repo.release.get("release_seq", 0),
            "target_root_hash": repo.release.get("root_hash"),
            "source": str(repo_source),
            "started_at": now_iso(),
            "cache_dir": str(cache),
            "parallel": max(1, int(parallel or 1)),
        })

        def prefetch_missing_chunks() -> Dict[str, Any]:
            workers = max(1, int(parallel or 1))
            if workers <= 1:
                return {"enabled": False, "workers": 1, "prefetched_chunks": 0, "skipped_existing_chunks": 0}
            seen: set[str] = set()
            ordered: list[str] = []
            for f in repo.files:
                for fc in f["chunks"]:
                    cid = fc["chunk_id"]
                    if cid not in seen:
                        seen.add(cid)
                        ordered.append(cid)
            skipped = 0
            missing: list[str] = []
            for cid in ordered:
                cp = cache / cid
                if cp.exists() and sha256_bytes(cp.read_bytes()) == cid:
                    skipped += 1
                    continue
                if allowed_patch_chunks is not None and cid not in allowed_patch_chunks:
                    # Leave the clearer patch-plan error to get_raw(), which can also
                    # use chunks already seeded from the install.
                    continue
                missing.append(cid)
            nonlocal downloaded_pack_bytes, downloaded_chunks
            repo.network_stats["parallel_workers"] = workers
            repo.network_stats["parallel_prefetch_candidates"] = len(missing)
            if not missing:
                return {"enabled": True, "workers": workers, "prefetched_chunks": 0, "skipped_existing_chunks": skipped}

            def download_one(cid: str) -> tuple[str, int]:
                cp = cache / cid
                # Another worker/process may have populated it while we were queuing.
                if cp.exists():
                    raw0 = cp.read_bytes()
                    if sha256_bytes(raw0) == cid:
                        return cid, 0
                raw = _read_packed_chunk(repo, repo.chunks[cid])
                tmp = cp.with_suffix(".tmp")
                tmp.write_bytes(raw)
                tmp.replace(cp)
                _cache_touch(cache, cid, len(raw))
                return cid, len(raw)

            done_count = 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(download_one, cid): cid for cid in missing}
                for fut in as_completed(futs):
                    cid = futs[fut]
                    try:
                        _cid, raw_len = fut.result()
                        if raw_len:
                            done_count += 1
                            downloaded_chunks += 1
                            downloaded_pack_bytes += int(repo.chunks[_cid]["pack_len"])
                    except Exception as e:
                        raise RuntimeError(f"parallel prefetch failed for chunk {cid}: {e}") from e
            repo.network_stats["parallel_prefetched_chunks"] = done_count
            return {"enabled": True, "workers": workers, "prefetched_chunks": done_count, "skipped_existing_chunks": skipped}

        _write_install_journal(install, status="running", phase="prefetch", staging_dir=str(staging), downloaded_chunks=downloaded_chunks, cache_hit_chunks=cache_hit_chunks)
        prefetch_report = prefetch_missing_chunks()
        _write_install_journal(install, status="running", phase="write-files", staging_dir=str(staging), downloaded_chunks=downloaded_chunks, cache_hit_chunks=cache_hit_chunks)

        for f in repo.files:
            ensure_safe_member(f["path"])
            target = staging / f["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as out:
                for fc in f["chunks"]:
                    out.write(get_raw(fc["chunk_id"]))
            if verify:
                actual = sha256_bytes(target.read_bytes())
                if actual != f["file_hash"] or target.stat().st_size != f["size"]:
                    raise IOError(f"verification failed for {f['path']}")
            _apply_file_metadata(target, f)

        manifest = {
            "schema": "CoreLangDistribution/InstalledManifest",
            "version": "2.0-alpha24",
            "installed_at": now_iso(),
            "release_id": repo.release.get("release_id"),
            "release_seq": int(repo.release.get("release_seq", 0)),
            "root_hash": repo.release.get("root_hash"),
            "source": str(repo_source),
            "tool": f"CoreLangDistribution {__version__}",
            "files": [{"path": f["path"], "size": f["size"], "file_hash": f["file_hash"], "mode": f.get("mode"), "mtime_ns": f.get("mtime_ns")} for f in repo.files],
        }
        write_json(meta / "installed.json", manifest)
        write_json(meta / "last_journal.json", {"schema": "CoreLangDistribution/InstallJournal", "version": "2.0-alpha24", "status": "complete", "phase": "pre-swap", "completed_at": now_iso(), "target_release_id": repo.release.get("release_id"), "target_release_seq": int(repo.release.get("release_seq", 0)), "target_root_hash": repo.release.get("root_hash")})
        _write_install_journal(install, status="running", phase="atomic-swap", staging_dir=str(staging), backup_dir=str(_backup_path(install)))
        # Mark state complete before atomic swap.
        state = read_json_local(meta / "fetch_state.json")
        state.update({"completed_at": now_iso(), "downloaded_chunks": downloaded_chunks, "cache_hit_chunks": cache_hit_chunks})
        write_json(meta / "fetch_state.json", state)

        if install.exists():
            backup = install.with_name(install.name + ".bak-cld2")
            if backup.exists():
                shutil.rmtree(backup)
            install.rename(backup)
            try:
                staging.rename(install)
            except Exception:
                if backup.exists() and not install.exists():
                    backup.rename(install)
                raise
            shutil.rmtree(backup, ignore_errors=True)
        else:
            staging.rename(install)
        if repo.is_remote:
            _save_mirror_state(repo)
        _clear_install_journal(install)
        return {
            "ok": True,
            "downloaded_pack_bytes": downloaded_pack_bytes,
            "downloaded_chunks": downloaded_chunks,
            "reused_cache_raw_bytes": reused_cache_raw_bytes,
            "cache_hit_chunks": cache_hit_chunks,
            "resumed_from_cache": cache_hit_chunks > 0,
            "seeded_chunks": seed_report["seeded_chunks"],
            "seeded_raw_bytes": seed_report["seeded_raw_bytes"],
            "seed_scanned_files": seed_report["scanned_files"],
            "files": len(repo.files),
            "installed_manifest": str(install / ".cld2" / "installed.json"),
            "rollback_guard": rollback_report,
            "patch_plan": {"used": patch_report is not None, "planned_chunks": (patch_report or {}).get("planned_chunks"), "planned_pack_bytes": (patch_report or {}).get("planned_pack_bytes")},
            "signature_checked": trust_key is not None or trusted_root is not None,
            "trusted_root_checked": trusted_root is not None,
            "trusted_root": trust_report,
            "trusted_root_sources": source_policy_report,
            "parallel_fetch": prefetch_report,
            "network": repo.network_stats if repo.is_remote else None,
            "cache_index": {"before": cache_index_before, "after": rebuild_cache_index(cache) if cache_dir is not None else None},
        }
    except Exception as e:
        try:
            _write_install_journal(install, status="failed", phase="cleanup", error=str(e), staging_dir=str(staging), backup_dir=str(backup) if backup else None, failed_at=now_iso())
        except Exception:
            pass
        shutil.rmtree(staging, ignore_errors=True)
        # If the atomic swap failed after moving the old install aside, restore it.
        if backup is not None and backup.exists() and not install.exists():
            backup.rename(install)
        raise
    finally:
        _release_install_lock(lock_path)
        if temp_cache:
            shutil.rmtree(cache, ignore_errors=True)
