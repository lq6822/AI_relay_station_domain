"""低频读取清单中的公开网页，保存可见文字、链接和实际访问记录；不调用模型。"""

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import requests


class PageText(HTMLParser):
    """提取网页文字和实际 href；忽略脚本、样式和图形，不执行页面代码。"""
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "svg"}:
            self.hidden += 1
        if tag == "meta" and attrs.get("name", "").lower() == "description":
            # 与正文使用同样的空白归一化，避免不可见空格导致逐字引用不一致。
            self.parts.append(" ".join(attrs.get("content", "").split()))
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])

    def handle_endtag(self, tag):
        if tag in {"script", "style", "svg"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(" ".join(data.split()))


def collect(url):
    """一次 GET，最多 20 秒和 2 MB；失败如实记录，不重试、不绕过访问限制。"""
    record = {"requested_url": url}
    try:
        with requests.get(url, timeout=20, stream=True,
                          headers={"User-Agent": "DomainIntelInterview/1.0 (public evidence research)"}) as response:
            record.update(status_code=response.status_code, final_url=response.url,
                          content_type=response.headers.get("content-type", ""))
            if not response.ok:
                record["error"] = f"HTTP {response.status_code}"
            elif not any(t in record["content_type"] for t in ("text/", "json", "xml")):
                record["error"] = "non-text response; body not collected"
            else:
                body = bytearray()
                for chunk in response.iter_content(32768):
                    body.extend(chunk)
                    if len(body) > 2_000_000:
                        raise ValueError("page exceeds 2 MB")
                record["body_sha256"] = hashlib.sha256(body).hexdigest()
                text = bytes(body).decode(response.encoding if response.encoding not in {None, "ISO-8859-1"} else "utf-8", errors="replace")
                if "html" in record["content_type"]:
                    parser = PageText()
                    parser.feed(text)
                    record["text"] = "\n".join(parser.parts)
                    record["links"] = list(dict.fromkeys(urljoin(response.url, link) for link in parser.links
                                                         if urljoin(response.url, link).startswith(("https://", "http://"))))
                else:
                    record["text"] = text
                    record["links"] = []
    except (requests.RequestException, ValueError) as exc:
        record["error"] = type(exc).__name__ if isinstance(exc, requests.RequestException) else str(exc)
    record["collected_at"] = datetime.now(timezone.utc).isoformat()
    return record


def main():
    """按清单顺序采集；已有记录不重复访问，每次请求后间隔两秒。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/research"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for source in json.loads(args.manifest.read_text(encoding="utf-8")):
        path = args.output / (source["source_id"] + ".json")
        if path.exists():
            continue
        record = {"source_id": source["source_id"], **collect(source["url"])}
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"id": source["source_id"], "status": record.get("status_code"),
                          "error": record.get("error"), "text_chars": len(record.get("text", ""))}), flush=True)
        time.sleep(2)


if __name__ == "__main__":
    main()
