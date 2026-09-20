"""Bounded local files and single-writer, atomic-per-file persistence."""

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ValidationError

from .contracts import Issue, Policy


def json_data(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [json_data(v) for v in value]
    if isinstance(value, dict):
        return {k: json_data(v) for k, v in value.items()}
    return value


def encode(value) -> str:
    return json.dumps(json_data(value), ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def input_fingerprint(candidate, materials, manifest):
    return digest({"candidate": candidate, "materials": sorted(materials, key=lambda m: m.material_id),
                   "configuration": manifest.configuration_digest,
                   "implementation": manifest.implementation_digest})


def validation_message(error: ValidationError) -> str:
    # Do not include raw rejected inputs or URLs/credentials in error logs.
    return "; ".join(
        f"{'.'.join(map(str, e['loc'])) or 'record'}: {e['type']}"
        for e in error.errors(include_input=False, include_url=False)
    )


def implementation_digest() -> str:
    folder = Path(__file__).parent
    return digest({p.name: p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.py"))})


def latest(records) -> dict:
    result = {}
    for record in records:
        if record.domain not in result or record.version > result[record.domain].version:
            result[record.domain] = record
    return result


class Workspace:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()

    def path(self, relative: str | Path) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("path must stay inside the workspace")
        return path

    def input_path(self, relative: str) -> Path:
        path = self.path(relative)
        if not path.is_relative_to(self.path("data/inputs")):
            raise ValueError("input must be under data/inputs")
        return path

    def run_dir(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
            raise ValueError("invalid run_id (use 1-64 letters, digits, _ or -)")
        if run_id.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)),
                               *(f"LPT{i}" for i in range(10))}:
            raise ValueError("reserved run_id")
        return self.path(Path("runs") / run_id)

    def run_file(self, run_id: str, name: str) -> Path:
        return self.path(self.run_dir(run_id) / name)

    def read_json(self, path: Path, model=None):
        path = self.path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        return model.model_validate(data) if model else data

    def read_records(self, run_id: str, name: str, model):
        path = self.run_file(run_id, name)
        if not path.exists():
            return []
        records = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                record = model.model_validate_json(line)
                if record.run_id != run_id:
                    raise ValueError("run_id mismatch")
                records.append(record)
            except (ValueError, ValidationError) as exc:
                raise ValueError(f"corrupt file {name}:{line_no}") from exc
        return records

    def read_input(self, relative: str, model, policy: Policy, run_id: str):
        path = self.input_path(relative)
        if path.stat().st_size > policy.max_input_bytes:
            raise ValueError(f"input exceeds byte limit: {relative}")
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        if len(lines) > policy.max_input_lines:
            raise ValueError(f"input exceeds line limit: {relative}")
        rows, issues = [], []
        for n, line in enumerate(lines, 1):
            try:
                rows.append(model.model_validate_json(line))
            except ValidationError as exc:
                issues.append(Issue(run_id=run_id, location=f"{relative}:{n}",
                                    stage="input", reason=validation_message(exc)))
        return rows, issues

    def write(self, path: Path, value, *, jsonl=False):
        path = self.path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(encode(v) + "\n" for v in value) if jsonl else encode(value) + "\n"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    def save_records(self, run_id: str, name: str, rows):
        self.write(self.run_file(run_id, name), rows, jsonl=True)

    @contextmanager
    def lock(self, run_id: str):
        folder = self.run_dir(run_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = self.run_file(run_id, ".writer.lock")
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise ValueError("batch is locked; check for an active writer before removing .writer.lock") from exc
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            yield
        finally:
            os.close(descriptor)
            path.unlink()
