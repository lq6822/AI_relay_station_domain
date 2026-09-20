"""将逐条选定的公开原文整理为程序输入；所有引用必须能在实际采集记录中找到。"""

import argparse
import json
from pathlib import Path

from relay_intel.candidates import normalize
from relay_intel.contracts import Lead, Material


def build_inputs(selection: list[dict], research: Path) -> tuple[list[Lead], list[Material]]:
    """校验选取的原文和实际链接，构造线索与材料；不联网、不写文件。"""
    leads, materials = [], []
    for index, item in enumerate(selection, 1):
        domain = normalize(item["domain"])
        for evidence in item["evidence"]:
            record = json.loads((research / (evidence["source_id"] + ".json")).read_text(encoding="utf-8"))
            source = record.get("final_url", record["requested_url"])
            values = dict(material_id=f"m-{index:03d}-{evidence['source_id']}", domain=domain,
                          source_url=source, collected_at=record["collected_at"],
                          evidence_state=evidence.get("evidence_state", "current"),
                          source_kind=evidence.get("source_kind", "direct"),
                          subject_relation=evidence.get("subject_relation", "exact"))
            if record.get("error"):
                # 访问失败归属本次请求的目标，重定向或错误响应不提供业务事实。
                values.update(source_url=record["requested_url"], access_status="failed", evidence_state="unknown",
                              failure_reason=f"GET {record['requested_url']}: {record['error']}；未取得可用正文。")
            else:
                quotes = evidence["quotes"]
                for quote in quotes:
                    if quote not in record["text"]:
                        raise ValueError(f"quote not found: {evidence['source_id']}: {quote[:60]}")
                links = evidence.get("links", [])
                if not all(url in record.get("links", []) or url in record["text"] for url in links):
                    raise ValueError(f"unobserved link: {evidence['source_id']}")
                excerpt = "\n\n".join(quotes)
                if links:
                    excerpt += "\n\n页面中观察到的链接：\n" + "\n".join(links)
                values.update(access_status="ok", excerpt=excerpt)
            materials.append(Material(**values))
        leads.append(Lead(lead_id=f"lead-{index:03d}", raw_value=domain,
                          source_url=item["discovery_source"], discovery_method=item["discovery_method"],
                          discovered_at=item.get("discovered_at", record["collected_at"])))
    return leads, materials


def main():
    """读取采集与摘录清单，输出主程序可以直接读取的两份 JSONL。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selection", type=Path)
    parser.add_argument("--research", type=Path, default=Path("data/research"))
    parser.add_argument("--output", type=Path, default=Path("data/inputs"))
    args = parser.parse_args()
    selection = json.loads(args.selection.read_text(encoding="utf-8"))
    leads, materials = build_inputs(selection, args.research)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, records in (("leads", leads), ("materials", materials)):
        (args.output / (name + ".jsonl")).write_text("".join(row.model_dump_json() + "\n" for row in records), encoding="utf-8")
    print(json.dumps({"domains": len(leads), "materials": len(materials)}))


if __name__ == "__main__":
    main()
