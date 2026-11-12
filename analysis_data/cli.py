"""查看合成分析资料的身份与基本数量，并执行现金流/赠款等值分析。"""
import argparse
import json
import sys
from pathlib import Path

from .io import load
from .domain import summarize
from . import cashflow


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["inspect", "analyze", "verify"])
    p.add_argument("file")
    p.add_argument("--report", help="verify 用：已发布报告 JSON 路径")
    a = p.parse_args()

    if a.command == "inspect":
        v, h = load(a.file)
        print(json.dumps({"manifest_sha256": h,
                          "summary": summarize(v, Path(a.file).parent)},
                         ensure_ascii=False, indent=2))
        return

    if a.command == "analyze":
        v, h = load(a.file)
        try:
            report = cashflow.analyze(v, h)
        except cashflow.ContractError as exc:
            print(json.dumps({"status": "blocked", "error": str(exc),
                              "manifest_sha256": h}, ensure_ascii=False, indent=2))
            sys.exit(1)
        print(json.dumps(cashflow.to_jsonable(report), ensure_ascii=False, indent=2))
        return

    # verify：报告绑定的合同摘要必须与当前清单一致，曲线/合同改动会改指纹
    v, h = load(a.file)
    if not a.report:
        print("verify 需要 --report", file=sys.stderr)
        sys.exit(2)
    report = json.loads(Path(a.report).read_text(encoding="utf8"))
    ok = cashflow.verify_binding(report, h)
    print(json.dumps({"binding_ok": ok}, ensure_ascii=False))
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
