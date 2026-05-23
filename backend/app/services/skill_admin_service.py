from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_skill_workspace_skills_dir
from app.db.models import AppSettings
from app.skills import skill_registry


_DOWNLOAD_LINK_RE = re.compile(
    r"""href=["']([^"']+)["'][^>]*>\s*Download\s+zip""",
    re.IGNORECASE,
)
_ALLOWED_SKILL_ID_RE = re.compile(r"[^a-zA-Z0-9._-]+")
MAX_SKILL_ARCHIVE_BYTES = 5 * 1024 * 1024
MAX_SKILL_ARCHIVE_EXTRACT_BYTES = 25 * 1024 * 1024
MAX_SKILL_ARCHIVE_FILE_COUNT = 200
_DEFAULT_TIMEOUT = 30.0
_SKILLHUB_DOWNLOAD_URL = "https://lightmake.site/api/v1/download"
_SKILLHUB_DOMAINS = {
    "skillhub.cn",
    "www.skillhub.cn",
    "skillhub.tencent.com",
    "www.skillhub.tencent.com",
}
_IGNORED_SUPPORT_FILES = {
    "SKILL.md",
    "_meta.json",
    "__pycache__",
}


def _workspace_skills_dir() -> Path:
    return get_skill_workspace_skills_dir()


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_skill_id(value: str) -> str:
    normalized = _ALLOWED_SKILL_ID_RE.sub("-", str(value or "").strip().lower())
    normalized = normalized.strip(".-_")
    if not normalized:
        raise ValueError("Unable to derive a valid skill id from the provided ClawHub source.")
    return normalized[:128]


class SkillAdminService:
    MAX_SKILL_ARCHIVE_EXTRACT_BYTES = MAX_SKILL_ARCHIVE_EXTRACT_BYTES
    MAX_SKILL_ARCHIVE_FILE_COUNT = MAX_SKILL_ARCHIVE_FILE_COUNT

    def _is_system_runtime(self, pkg_or_id: Any) -> bool:
        return skill_registry.is_system_runtime(pkg_or_id)

    def _validate_archive_bytes(self, archive_bytes: bytes, *, source_label: str) -> None:
        if not archive_bytes:
            raise ValueError(f"{source_label}skill archive is empty.")
        if len(archive_bytes) > MAX_SKILL_ARCHIVE_BYTES:
            raise ValueError(
                f"{source_label}skill archive is too large and exceeds the 5MB safety limit."
            )

    def _get_or_create_settings(self, db: Session) -> AppSettings:
        instance = db.scalar(select(AppSettings).limit(1))
        if instance is None:
            from app.services.settings_service import settings_service

            instance = settings_service.get_or_create_settings(db)
        return instance

    def _get_disabled_skill_ids(self, db: Session) -> set[str]:
        settings = self._get_or_create_settings(db)
        raw = str(settings.disabled_skill_ids_json or "[]").strip() or "[]"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return set()
        if not isinstance(parsed, list):
            return set()
        return {
            _normalize_skill_id(item)
            for item in parsed
            if isinstance(item, str) and item.strip()
            and not self._is_system_runtime(_normalize_skill_id(item))
        }

    def _save_disabled_skill_ids(self, db: Session, disabled_ids: set[str]) -> None:
        settings = self._get_or_create_settings(db)
        persisted_ids = {
            skill_id for skill_id in disabled_ids if not self._is_system_runtime(skill_id)
        }
        settings.disabled_skill_ids_json = json.dumps(
            sorted(persisted_ids),
            ensure_ascii=False,
        )
        db.add(settings)
        db.commit()
        db.refresh(settings)

    def _sync_persisted_state(self, db: Session) -> set[str]:
        disabled_ids = self._get_disabled_skill_ids(db)
        installed_ids = {pkg.id for pkg in skill_registry.all_packages()}
        skill_registry.set_disabled(disabled_ids & installed_ids)
        return disabled_ids

    def apply_persisted_state(self, db: Session) -> None:
        self._sync_persisted_state(db)

    def _reload_and_apply_state(self, db: Session) -> None:
        skill_registry.reload()
        self.apply_persisted_state(db)

    def _extract_run_types(self, pkg: Any) -> list[str]:
        return list(getattr(pkg, "run_types", []) or [])

    def _extract_category(self, pkg: Any) -> str | None:
        meta = pkg.metadata.get("metadata")
        if not isinstance(meta, dict):
            return None
        for key in ("aniu", "openclaw"):
            payload = meta.get(key)
            if isinstance(payload, dict):
                category = payload.get("category")
                if isinstance(category, str) and category.strip():
                    return category.strip()
        return None

    def _list_support_files(self, pkg: Any) -> list[str]:
        files: list[str] = []
        for path in pkg.path.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(pkg.path).as_posix()
            if relative in _IGNORED_SUPPORT_FILES:
                continue
            files.append(relative)
        return sorted(files)[:12]

    def _build_compatibility(self, pkg: Any) -> tuple[str, str, list[str]]:
        if self._is_system_runtime(pkg):
            return "native", "System runtime tools that stay enabled to support all skills.", []

        issues: list[str] = []
        requires = getattr(pkg, "requires", {}) or {}
        bins = requires.get("bins") if isinstance(requires.get("bins"), list) else []
        envs = requires.get("env") if isinstance(requires.get("env"), list) else []

        if bins:
            issues.append(
                "Declared external binary dependencies: "
                + ", ".join(str(item) for item in bins if str(item).strip())
                + "; please ensure these commands are available before execution."
            )
        if envs:
            issues.append(
                "Declared environment variable dependencies: "
                + ", ".join(str(item) for item in envs if str(item).strip())
                + "; please configure these variables before execution."
            )

        if pkg.skill is not None and not issues:
            return "native", "Native Aniu skill with directly callable tools.", issues
        if pkg.skill is not None:
            return "needs_attention", "Skill can be loaded, but there are extra runtime prerequisites to verify.", issues
        if issues:
            return "needs_attention", "Prompt-style skill is supported, but runtime prerequisites still need attention.", issues
        return "prompt_only", "No Python handler was found; the skill will run through the shared skill runtime tools.", issues

    def _build_skill_info(self, pkg: Any, *, enabled: bool) -> dict[str, Any]:
        meta_payload = _read_json_file(pkg.path / "_meta.json")
        compatibility_level, compatibility_summary, issues = self._build_compatibility(pkg)
        published_at = meta_payload.get("publishedAt")
        published_at_value = None
        if isinstance(published_at, (int, float)) and published_at > 0:
            published_at_value = datetime.fromtimestamp(published_at / 1000, tz=UTC)

        clawhub_slug = meta_payload.get("slug")
        clawhub_slug_value = (
            str(clawhub_slug).strip() if isinstance(clawhub_slug, str) and clawhub_slug.strip() else None
        )
        source_url = meta_payload.get("source_url")
        source_url_value = (
            str(source_url).strip() if isinstance(source_url, str) and source_url.strip() else None
        )
        return {
            "id": pkg.id,
            "name": pkg.name,
            "description": pkg.description,
            "location": str(pkg.path.resolve()),
            "source": pkg.source,
            "role": getattr(pkg, "role", "standard"),
            "enabled": enabled,
            "can_disable": bool(getattr(pkg, "can_disable", False)),
            "can_delete": bool(getattr(pkg, "can_delete", False)),
            "always_enabled": bool(getattr(pkg, "always_enabled", False)),
            "has_handler": pkg.skill is not None,
            "tool_names": sorted(pkg.tool_names()),
            "run_types": self._extract_run_types(pkg),
            "category": self._extract_category(pkg),
            "compatibility_level": compatibility_level,
            "compatibility_summary": compatibility_summary,
            "issues": issues,
            "support_files": self._list_support_files(pkg),
            "clawhub_slug": clawhub_slug_value,
            "clawhub_version": (
                str(meta_payload.get("version")).strip()
                if meta_payload.get("version") is not None
                else None
            ),
            "clawhub_url": (
                source_url_value
                if source_url_value
                else (
                    f"https://clawhub.ai/skills/{clawhub_slug_value}"
                    if clawhub_slug_value
                    else None
                )
            ),
            "published_at": published_at_value,
        }

    def _build_skill_list_item(self, pkg: Any, *, enabled: bool) -> dict[str, Any]:
        return {
            "id": pkg.id,
            "name": pkg.name,
            "description": pkg.description,
            "source": pkg.source,
            "role": getattr(pkg, "role", "standard"),
            "enabled": enabled,
            "can_disable": bool(getattr(pkg, "can_disable", False)),
            "can_delete": bool(getattr(pkg, "can_delete", False)),
            "always_enabled": bool(getattr(pkg, "always_enabled", False)),
        }

    def _sorted_packages(self) -> list[Any]:
        return sorted(
            skill_registry.all_packages(),
            key=lambda item: (
                0 if self._is_system_runtime(item) else 1,
                0 if item.source == "builtin" else 1,
                item.name.lower(),
            ),
        )

    def list_skills(self, db: Session) -> list[dict[str, Any]]:
        disabled_ids = self._sync_persisted_state(db)
        packages = self._sorted_packages()
        return [
            self._build_skill_list_item(
                pkg,
                enabled=(pkg.always_enabled or pkg.id not in disabled_ids),
            )
            for pkg in packages
        ]

    def _find_skill(self, skill_id: str) -> Any:
        normalized = _normalize_skill_id(skill_id)
        for pkg in skill_registry.all_packages():
            if pkg.id == normalized:
                return pkg
        raise LookupError(f"Skill not found: {skill_id}")

    def set_enabled(self, db: Session, *, skill_id: str, enabled: bool) -> dict[str, Any]:
        pkg = self._find_skill(skill_id)
        if not getattr(pkg, "can_disable", False):
            if not enabled:
                raise ValueError("System runtime skills cannot be disabled.")
            return self._build_skill_info(pkg, enabled=bool(getattr(pkg, "always_enabled", True)))
        disabled_ids = self._get_disabled_skill_ids(db)
        if enabled:
            disabled_ids.discard(pkg.id)
        else:
            disabled_ids.add(pkg.id)
        self._save_disabled_skill_ids(db, disabled_ids)
        self.apply_persisted_state(db)
        return self._build_skill_info(pkg, enabled=enabled)

    def delete_skill(self, db: Session, *, skill_id: str) -> None:
        pkg = self._find_skill(skill_id)
        if not getattr(pkg, "can_delete", False):
            raise ValueError("Built-in skills cannot be deleted.")

        workspace_dir = _workspace_skills_dir().resolve()
        target_dir = pkg.path.resolve()
        try:
            target_dir.relative_to(workspace_dir)
        except ValueError as exc:
            raise ValueError("Only workspace skills inside the managed skill directory can be deleted.") from exc

        disabled_ids = self._get_disabled_skill_ids(db)
        disabled_ids.discard(pkg.id)
        self._save_disabled_skill_ids(db, disabled_ids)

        if target_dir.exists():
            shutil.rmtree(target_dir)
        self._reload_and_apply_state(db)

    def reload(self, db: Session) -> list[dict[str, Any]]:
        skill_registry.reload()
        disabled_ids = self._sync_persisted_state(db)
        packages = self._sorted_packages()
        return [
            self._build_skill_list_item(
                pkg,
                enabled=(pkg.always_enabled or pkg.id not in disabled_ids),
            )
            for pkg in packages
        ]

    def _resolve_target_dir(self, skill_id: str) -> Path:
        workspace_dir = _workspace_skills_dir()
        workspace_dir.mkdir(parents=True, exist_ok=True)

        builtin_ids = {
            pkg.id for pkg in skill_registry.all_packages() if pkg.source == "builtin"
        }
        if skill_id in builtin_ids:
            raise ValueError(f"导入的技能标识与内置技能冲突：{skill_id}")

        target_dir = workspace_dir / skill_id
        if target_dir.exists():
            raise FileExistsError(
                f"Skill already exists: {skill_id}. Current version does not support overwrite import; please handle the existing directory first."
            )
        return target_dir

    def _resolve_skillhub_slug(self, slug_or_url: str) -> str:
        value = str(slug_or_url or "").strip()
        if not value:
            raise ValueError("Please provide a valid SkillHub skill slug or URL.")

        if "://" not in value:
            return _normalize_skill_id(value)

        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("SkillHub URL must start with http:// or https://.")
        if parsed.netloc not in _SKILLHUB_DOMAINS:
            raise ValueError("Only skills from skillhub.cn / skillhub.tencent.com are currently supported.")

        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ValueError("Could not identify a skill slug from the provided SkillHub URL.")
        return _normalize_skill_id(parts[-1])

    def _annotate_skillhub_metadata(self, source_root: Path, *, slug: str) -> None:
        meta_path = source_root / "_meta.json"
        payload = _read_json_file(meta_path)
        source_url = f"https://skillhub.cn/skills/{slug}"
        changed = False
        if payload.get("slug") != slug:
            payload["slug"] = slug
            changed = True
        if payload.get("source_url") != source_url:
            payload["source_url"] = source_url
            changed = True
        if payload.get("marketplace") != "skillhub":
            payload["marketplace"] = "skillhub"
            changed = True
        if not changed:
            return
        meta_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _download_skillhub_archive(self, slug_or_url: str) -> tuple[str, bytes]:
        slug = self._resolve_skillhub_slug(slug_or_url)

        with httpx.Client(follow_redirects=True, timeout=_DEFAULT_TIMEOUT) as client:
            try:
                response = client.get(_SKILLHUB_DOWNLOAD_URL, params={"slug": slug})
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Failed to download SkillHub skill archive: {exc}") from exc

        archive_bytes = response.content
        try:
            self._validate_archive_bytes(archive_bytes, source_label="Downloaded ")
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        return slug, archive_bytes

    def _resolve_skill_root_from_directory(
        self,
        base_dir: Path,
        *,
        requested_slug: str,
    ) -> Path:
        candidates: list[Path] = []
        if (base_dir / "SKILL.md").is_file():
            candidates.append(base_dir)

        for skill_file in base_dir.rglob("SKILL.md"):
            parent = skill_file.parent
            if parent == base_dir:
                continue
            candidates.append(parent)

        unique_candidates: list[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            unique_candidates.append(candidate)

        if not unique_candidates:
            raise RuntimeError("SkillHub installation completed, but no SKILL.md was found in the output.")

        for candidate in unique_candidates:
            if _normalize_skill_id(candidate.name) == requested_slug:
                return candidate
            meta_payload = _read_json_file(candidate / "_meta.json")
            meta_slug = meta_payload.get("slug")
            if isinstance(meta_slug, str) and _normalize_skill_id(meta_slug) == requested_slug:
                return candidate

        if len(unique_candidates) == 1:
            return unique_candidates[0]

        raise RuntimeError(
            "SkillHub installation completed, but multiple candidate skill directories were found and the target could not be determined."
        )

    def _install_from_source_root(self, *, skill_id: str, source_root: Path) -> None:
        target_dir = self._resolve_target_dir(skill_id)
        shutil.copytree(source_root, target_dir)


    def import_from_skillhub(self, db: Session, slug_or_url: str) -> dict[str, Any]:
        slug, archive_bytes = self._download_skillhub_archive(slug_or_url)
        with tempfile.TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            source_root = self._extract_archive_to_temp(archive_bytes, temp_dir)
            self._annotate_skillhub_metadata(source_root, slug=slug)
            self._install_from_source_root(skill_id=slug, source_root=source_root)

        return self._finalize_import(db, skill_id=slug)

    def _resolve_clawhub_page_url(self, slug_or_url: str) -> tuple[str, str]:
        value = str(slug_or_url or "").strip()
        if not value:
            raise ValueError("Please provide a valid ClawHub skill slug or URL.")

        if "://" not in value:
            slug = _normalize_skill_id(value)
            return slug, f"https://clawhub.ai/skills/{slug}"

        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("ClawHub URL must start with http:// or https://.")
        if parsed.netloc not in {"clawhub.ai", "www.clawhub.ai"}:
            raise ValueError("Only skills from clawhub.ai are currently supported.")

        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ValueError("Could not identify a skill slug from the provided URL.")
        if parts[0] == "skills" and len(parts) >= 2:
            slug = parts[1]
        else:
            slug = parts[-1]
        normalized_slug = _normalize_skill_id(slug)
        return normalized_slug, f"https://clawhub.ai/skills/{normalized_slug}"

    def _download_clawhub_archive(self, slug_or_url: str) -> tuple[str, bytes]:
        slug, page_url = self._resolve_clawhub_page_url(slug_or_url)

        with httpx.Client(follow_redirects=True, timeout=_DEFAULT_TIMEOUT) as client:
            try:
                page_response = client.get(page_url)
                page_response.raise_for_status()
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Failed to open ClawHub page: {exc}") from exc

            match = _DOWNLOAD_LINK_RE.search(page_response.text)
            if not match:
                raise RuntimeError("No downloadable zip link was found on the ClawHub page.")

            download_url = urljoin(str(page_response.url), match.group(1))
            try:
                archive_response = client.get(download_url)
                archive_response.raise_for_status()
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Failed to download ClawHub skill archive: {exc}") from exc

        archive_bytes = archive_response.content
        try:
            self._validate_archive_bytes(archive_bytes, source_label="Downloaded ")
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        return slug, archive_bytes

    def _extract_archive_to_temp(self, archive_bytes: bytes, temp_dir: Path) -> Path:
        archive_path = temp_dir / "skill.zip"
        archive_path.write_bytes(archive_bytes)
        extract_root = temp_dir / "unzipped"
        extract_root.mkdir(parents=True, exist_ok=True)

        try:
            with zipfile.ZipFile(archive_path) as zf:
                total_uncompressed_size = 0
                file_count = 0
                extract_root_resolved = extract_root.resolve()
                for member in zf.infolist():
                    member_path = Path(member.filename)
                    if member_path.is_absolute():
                        raise ValueError("Skill archive contains an absolute path and was rejected.")
                    destination = (extract_root / member_path).resolve()
                    if (
                        extract_root_resolved not in destination.parents
                        and destination != extract_root_resolved
                    ):
                        raise ValueError("Skill archive contains a path traversal entry and was rejected.")
                    if member.is_dir():
                        continue
                    file_count += 1
                    if file_count > self.MAX_SKILL_ARCHIVE_FILE_COUNT:
                        raise ValueError(
                            "Skill archive contains too many files and exceeds the safety limit."
                        )
                    total_uncompressed_size += max(int(member.file_size or 0), 0)
                    if total_uncompressed_size > self.MAX_SKILL_ARCHIVE_EXTRACT_BYTES:
                        raise ValueError(
                            "Skill archive expands to an unsafe size after extraction and was rejected."
                        )
                zf.extractall(extract_root)
        except zipfile.BadZipFile as exc:
            raise ValueError("Skill archive is not a valid zip file.") from exc
        except ValueError as exc:
            message = str(exc)
            if "unsafe size after extraction" in message:
                raise ValueError("技能压缩包解压后体积过大，已超出安全限制。") from exc
            if "too many files" in message:
                raise ValueError("技能压缩包内文件数量过多，已超出安全限制。") from exc
            raise

        candidate_root = extract_root
        if not (candidate_root / "SKILL.md").is_file():
            child_dirs = [path for path in extract_root.iterdir() if path.is_dir()]
            child_files = [path for path in extract_root.iterdir() if path.is_file()]
            if len(child_dirs) == 1 and not child_files:
                candidate_root = child_dirs[0]

        if not (candidate_root / "SKILL.md").is_file():
            raise ValueError("SKILL.md was not found in the uploaded skill archive.")
        return candidate_root

    def _derive_skill_id_from_upload(
        self,
        *,
        source_root: Path,
        filename: str,
    ) -> str:
        meta_payload = _read_json_file(source_root / "_meta.json")
        candidates: list[str] = []

        meta_slug = meta_payload.get("slug")
        if isinstance(meta_slug, str) and meta_slug.strip():
            candidates.append(meta_slug)

        filename_stem = Path(str(filename or "").strip()).stem
        if filename_stem:
            candidates.append(filename_stem)

        if source_root.name not in {"", ".", "unzipped"}:
            candidates.append(source_root.name)

        for candidate in candidates:
            try:
                return _normalize_skill_id(candidate)
            except ValueError:
                continue

        raise ValueError("Could not derive a skill id from the zip file. Please check the filename or provide _meta.json.")

    def _install_archive(self, *, skill_id: str, archive_bytes: bytes) -> None:
        target_dir = self._resolve_target_dir(skill_id)
        with tempfile.TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            source_root = self._extract_archive_to_temp(archive_bytes, temp_dir)
            shutil.copytree(source_root, target_dir)

    def _finalize_import(self, db: Session, *, skill_id: str) -> dict[str, Any]:
        disabled_ids = self._get_disabled_skill_ids(db)
        disabled_ids.add(skill_id)
        self._save_disabled_skill_ids(db, disabled_ids)
        self._reload_and_apply_state(db)
        pkg = self._find_skill(skill_id)
        return self._build_skill_info(pkg, enabled=False)

    def import_from_clawhub(self, db: Session, slug_or_url: str) -> dict[str, Any]:
        slug, archive_bytes = self._download_clawhub_archive(slug_or_url)
        self._install_archive(skill_id=slug, archive_bytes=archive_bytes)
        return self._finalize_import(db, skill_id=slug)

    def import_from_zip(
        self,
        db: Session,
        *,
        filename: str,
        archive_bytes: bytes,
    ) -> dict[str, Any]:
        self._validate_archive_bytes(archive_bytes, source_label="Uploaded ")

        with tempfile.TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            source_root = self._extract_archive_to_temp(archive_bytes, temp_dir)
            skill_id = self._derive_skill_id_from_upload(
                source_root=source_root,
                filename=filename,
            )

        self._install_archive(skill_id=skill_id, archive_bytes=archive_bytes)
        return self._finalize_import(db, skill_id=skill_id)


skill_admin_service = SkillAdminService()
