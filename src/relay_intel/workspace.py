"""只负责本地文件读写；每批数据集中保存到一个 batch.json。"""

import json
import os
import re
import tempfile
from pathlib import Path

from pydantic import BaseModel, ValidationError

from .contracts import AIConfig, Batch, Issue


def json_data(value):
    """将 Pydantic 对象递归转为 JSON 可写值；时间使用带时区的 ISO 字符串。"""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [json_data(item) for item in value]
    if isinstance(value, dict):
        return {key: json_data(item) for key, item in value.items()}
    return value


def encode(value) -> str:
    """统一序列化材料、批次和工具响应，保留中文并拒绝非有限数值。"""
    return json.dumps(json_data(value), ensure_ascii=False, allow_nan=False)


def validation_message(error: ValidationError) -> str:
    # 不输出被拒绝的原始内容，避免把用户误填的密钥写入日志。
    """只提取字段位置和错误类型，不回显可能包含敏感内容的原始输入。"""
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'record'}: {e['type']}"
                     for e in error.errors(include_input=False, include_url=False))


class Workspace:
    """本项目目录下的文件操作；不负责业务判定和批次调度。"""
    def __init__(self, root: Path | str):
        """固定工作目录，后续相对路径均以此为基准。"""
        self.root = Path(root).resolve()

    def path(self, relative: str | Path) -> Path:
        """将输入路径解析为工作目录内的绝对路径，拒绝越界。"""
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("path must stay inside the workspace")
        return path

    def batch_dir(self, run_id: str) -> Path:
        """检查批次名并返回 runs/<run_id>，这里只计算路径、不创建目录。"""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
            raise ValueError("invalid run_id (use 1-64 letters, digits, _ or -)")
        if re.fullmatch(r"CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]", run_id.upper()):
            raise ValueError("reserved run_id")
        return self.path(Path("runs") / run_id)

    def read_ai_config(self) -> AIConfig:
        """读取唯一的本地 AI 配置；地址、模型和 Key 都在这个文件中编辑。"""
        path = self.path("config/ai.json")
        if not path.exists():
            raise ValueError("copy config/ai.example.json to config/ai.json and fill in api_key")
        return AIConfig.model_validate_json(path.read_text(encoding="utf-8-sig"))

    def read_input(self, relative: str, model) -> tuple[list, list[Issue]]:
        """逐行校验 JSONL，返回有效记录和含行号的问题；整体文件错误上报。"""
        path = self.path(relative)
        if path.stat().st_size > 10_000_000:
            raise ValueError(f"input exceeds 10 MB: {relative}")
        rows, issues = [], []
        for line_no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            try:
                rows.append(model.model_validate_json(line))
            except ValidationError as exc:
                issues.append(Issue(location=f"{relative}:{line_no}", reason=validation_message(exc)))
        return rows, issues

    def write(self, path: Path, value, *, jsonl=False):
        """写入临时文件后替换单个目标文件；写失败保留原文件并上报。"""
        path = self.path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(encode(row) + "\n" for row in value) if jsonl else encode(value) + "\n"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(body)
            os.replace(temporary, path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    def save_batch(self, batch: Batch):
        """将完整批次保存为一个 batch.json，避免多个阶段文件互相对版本。"""
        self.write(self.batch_dir(batch.run_id) / "batch.json", batch)

    def load_batch(self, run_id: str) -> Batch:
        """严格读取当前格式的批次；不迁移旧格式，也不自动修复损坏文件。"""
        batch = Batch.model_validate_json((self.batch_dir(run_id) / "batch.json").read_text(encoding="utf-8"))
        if batch.run_id != run_id:
            raise ValueError("batch run_id mismatch")
        return batch
