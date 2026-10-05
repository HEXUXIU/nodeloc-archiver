import json
import pathlib
from collections import Counter

root = pathlib.Path("data/topics")
metas = sorted(root.glob("*/metadata.json"))

failures = json.loads(pathlib.Path("data/failures.json").read_text(encoding="utf-8")) if pathlib.Path("data/failures.json").exists() else []

restricted = []
no_sitemap = []
incomplete = []
post_types = Counter()
statuses = Counter()

for m in metas:
    try:
        md = json.loads(m.read_text(encoding="utf-8"))
    except Exception as exc:
        incomplete.append((m.parent.name, f"metadata 解析失败: {exc}"))
        continue
    if md.get("restricted"):
        restricted.append(m.parent.name)
    if not md.get("sitemap"):
        no_sitemap.append(m.parent.name)
    if md.get("complete") is not True:
        incomplete.append((m.parent.name, "complete 不为 true"))
    statuses[md.get("status")] += 1

print("=== 总体 ===")
print(f"主题总数: {len(metas)}")
print(f"status: {dict(statuses)}")
print(f"权限受限主题: {len(restricted)}")
print(f"无 sitemap 来源: {len(no_sitemap)}")
print(f"complete != true: {len(incomplete)}")

print()
print("=== 权限受限主题明细（仅列前 30，共 %d 个）===" % len(restricted))
for tid in restricted[:30]:
    print(f"  {tid}")

print()
print("=== failures.json ===")
print(f"条目数: {len(failures)}")
if failures:
    kinds = Counter()
    for f in failures:
        err = str(f.get("error", ""))
        if "429" in err:
            kinds["429 限流"] += 1
        elif "404" in err:
            kinds["404 不存在"] += 1
        elif "403" in err:
            kinds["403 禁止"] += 1
        elif "timeout" in err.lower():
            kinds["超时"] += 1
        else:
            kinds["其他"] += 1
    for k, n in kinds.most_common():
        print(f"  {k}: {n}")
    for f in failures[:10]:
        print(f"  - {f.get('topic_id')}: {str(f.get('error'))[:150]}")
else:
    print("  空，无失败记录")

print()
print("=== 结论 ===")
print(f"公开可访问且成功归档: {len(metas) - len(restricted)}")
print(f"仅存权限占位: {len(restricted)}")
print(f"实际抓取失败: {len(failures)}")

pathlib.Path("data/final_report.json").write_text(
    json.dumps(
        {
            "total_topics": len(metas),
            "statuses": dict(statuses),
            "restricted_count": len(restricted),
            "restricted_ids": restricted,
            "no_sitemap_ids": no_sitemap,
            "incomplete": incomplete,
            "failures": failures,
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)
print("已写入 data/final_report.json")