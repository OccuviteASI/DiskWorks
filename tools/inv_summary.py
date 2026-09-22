"""Print a one-screen summary of an inventory JSON (from stdin or a file)."""
import json
import sys

inv = json.load(open(sys.argv[1], encoding="utf-8")) if len(sys.argv) > 1 else json.load(sys.stdin)
print("platform", inv["platform"], "hash", inv["hash"], "disks", len(inv["disks"]), "elevated", inv.get("elevated"), "note", inv.get("note"))
for d in inv["disks"]:
    print(f"{d['name']}: {d['model']!r} bus={d['bus']} media={d['media']} size={d['size']/2**30:.1f}GiB table={d['table']} "
          f"rem={d['removable']} sys={d['system']} boot={d['boot']} locked={d['locked']} wholeFs={d.get('wholeDiskFs')}")
    for s in d["segments"]:
        if s["kind"] == "gap":
            print(f"   GAP  start={s['start']/2**20:.1f}MiB size={s['size']/2**20:.1f}MiB")
        else:
            p = next(x for x in d["partitions"] if x["id"] == s["id"])
            fl = {k: v for k, v in p["flags"].items() if v}
            print(f"   {p['id']} start={p['start']/2**20:.1f}MiB size={p['size']/2**30:.2f}GiB type={p['typeName']!r} fs={p['fs']} "
                  f"letter={p['letter']} label={p['label']!r} mps={p['mountpoints']} used={p['used']} flags={fl} locked={p['locked']} allow={p['allow']}")
