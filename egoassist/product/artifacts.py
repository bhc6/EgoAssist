"""Task-scoped local artifact resolution with path and ownership checks."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from .contracts import ArtifactDescriptor


class LocalArtifactPort:
    def __init__(self, catalog: list[Mapping[str, Any]]) -> None:
        self._artifacts: dict[str, dict[str, Any]] = {}
        for raw in catalog:
            artifact_id = str(raw.get("id", "")).strip()
            root = Path(raw.get("root", "")).resolve()
            path = (root / str(raw.get("path", ""))).resolve()
            if not artifact_id or artifact_id in self._artifacts:
                raise ValueError("artifact IDs must be non-empty and unique")
            if root not in path.parents or not path.is_file():
                raise ValueError(f"artifact path must be an existing file under its Skill: {artifact_id}")
            media_type = str(raw.get("mediaType", ""))
            if media_type not in {"application/pdf", "image/png", "image/jpeg", "image/webp"}:
                raise ValueError(f"unsupported artifact media type: {media_type}")
            self._artifacts[artifact_id] = {
                **dict(raw),
                "id": artifact_id,
                "path": path,
                "mediaType": media_type,
                "checksum": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            }

    def list_for_task(
        self,
        *,
        owner_id: str,
        skill: str | None,
        task_id: str | None,
        step_id: str | None,
    ) -> list[dict[str, Any]]:
        values = []
        for item in self._artifacts.values():
            if skill and str(item.get("skill")) != skill:
                continue
            tasks = [str(value) for value in item.get("tasks", [])]
            if task_id and tasks and task_id not in tasks and "*" not in tasks:
                continue
            steps = [str(value) for value in item.get("steps", [])]
            if step_id and steps and step_id not in steps and "*" not in steps:
                continue
            descriptor = self._descriptor(item, owner_id=owner_id, step_id=step_id)
            values.append({
                **descriptor.model_dump(mode="json"),
                "title": str(item.get("title") or item["id"]),
                "skill": str(item.get("skill", "")),
                "tasks": tasks,
                "steps": steps,
                "previewUrl": f"/api/artifacts/{item['id']}/content",
            })
        return values

    async def resolve(self, artifact_id: str, *, owner_id: str) -> ArtifactDescriptor:
        item = self._artifacts.get(str(artifact_id))
        if item is None:
            raise ValueError("unknown artifact")
        return self._descriptor(item, owner_id=owner_id, step_id=None)

    async def preview(self, artifact: ArtifactDescriptor) -> Mapping[str, Any]:
        item = self._artifacts.get(artifact.artifact_id)
        if item is None:
            raise ValueError("unknown artifact")
        return {
            "artifact": artifact.model_dump(mode="json"),
            "title": str(item.get("title") or artifact.artifact_id),
            "previewUrl": f"/api/artifacts/{artifact.artifact_id}/content",
        }

    def content(self, artifact_id: str) -> tuple[Path, str]:
        item = self._artifacts.get(str(artifact_id))
        if item is None:
            raise ValueError("unknown artifact")
        return Path(item["path"]), str(item["mediaType"])

    @staticmethod
    def _descriptor(item: Mapping[str, Any], *, owner_id: str, step_id: str | None) -> ArtifactDescriptor:
        path = Path(item["path"])
        return ArtifactDescriptor(
            artifact_id=str(item["id"]),
            owner_id=str(owner_id),
            kind="document" if item["mediaType"] == "application/pdf" else "image",
            uri=path.as_uri(),
            media_type=str(item["mediaType"]),
            checksum=str(item["checksum"]),
            step_id=step_id,
            provenance={
                "skill": item.get("skill"),
                "tasks": list(item.get("tasks", [])),
                "steps": list(item.get("steps", [])),
                "source": item.get("source", "bundled-skill-reference"),
            },
        )
