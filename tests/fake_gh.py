"""A fake `gh` CLI for tests: keeps issues, a Project board and PRs in a JSON file (FAKE_GH_STATE)."""
import json
import os
import sys

path = os.environ["FAKE_GH_STATE"]
st = json.load(open(path)) if os.path.exists(path) else {}
st.setdefault("issues", {}); st.setdefault("items", {}); st.setdefault("calls", []); st.setdefault("comments", [])
st.setdefault("fields", []); st.setdefault("prs", {})
args = sys.argv[1:]
st["calls"].append(args)
stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
out = ""


def flag(name, default=""):
    return args[args.index(name) + 1] if name in args else default


if args[:2] == ["api", "user"]:
    out = "pankajneema\n"
elif args[:2] == ["project", "create"]:
    out = json.dumps({"number": 7, "url": "https://github.com/users/pankajneema/projects/7", "id": "PVT_7"})
elif args[:2] == ["project", "view"]:
    out = json.dumps({"id": "PVT_7", "url": "https://github.com/users/pankajneema/projects/7"})
elif args[:2] == ["project", "field-list"]:
    out = json.dumps({"fields": [{"id": "F_status", "name": "Status", "options": [
        {"id": "o_todo", "name": "Todo"}, {"id": "o_prog", "name": "In Progress"}, {"id": "o_done", "name": "Done"}]}]
        + st["fields"]})
elif args[:2] == ["project", "field-create"]:
    name = flag("--name")
    f = {"id": "F_" + name.lower(), "name": name}
    if "--single-select-options" in args:
        f["options"] = [{"id": f"o_{name.lower()}_{o.lower().replace(' ', '')}", "name": o}
                        for o in flag("--single-select-options").split(",")]
    st["fields"].append(f)
    out = json.dumps(f)
elif args[:2] == ["issue", "create"]:
    n = len(st["issues"]) + 1
    url = f"https://github.com/{flag('--repo')}/issues/{n}"
    st["issues"][str(n)] = {"title": flag("--title"), "body": stdin, "state": "open"}
    out = url + "\n"
elif args[:2] == ["issue", "edit"]:
    st["issues"][args[2]].update(title=flag("--title"), body=stdin)
elif args[:2] in (["issue", "close"], ["issue", "reopen"]):
    st["issues"][args[2]]["state"] = "closed" if args[1] == "close" else "open"
elif args[:2] == ["project", "item-add"]:
    iid = f"PVTI_{len(st['items']) + 1}"
    st["items"][iid] = {"id": iid, "url": flag("--url")}
    out = json.dumps({"id": iid})
elif args[:2] == ["project", "item-edit"]:
    item, fid = st["items"][flag("--id")], flag("--field-id")
    field = next(f for f in st["fields"] + [{"id": "F_status", "name": "Status", "options": [
        {"id": "o_todo", "name": "Todo"}, {"id": "o_prog", "name": "In Progress"}, {"id": "o_done", "name": "Done"}]}]
        if f["id"] == fid)
    key = field["name"].lower()
    if "--clear" in args:
        item.pop(key, None)
    elif "--single-select-option-id" in args:
        item[key] = next(o["name"] for o in field["options"] if o["id"] == flag("--single-select-option-id"))
    else:
        item[key] = flag("--date") or flag("--text")
elif args[:2] == ["project", "item-list"]:
    out = json.dumps({"items": list(st["items"].values())})
elif args[0] == "api" and "issues/comments" in args[1]:
    out = json.dumps(st["comments"])
elif args[:2] == ["pr", "create"]:
    n = len(st["prs"]) + 1
    url = f"https://github.com/acme/site/pull/{n}"
    st["prs"][url] = {"head": flag("--head"), "title": flag("--title"), "state": "open"}
    out = url + "\n"
elif args[:2] in (["pr", "merge"], ["pr", "close"]):
    st["prs"][args[2]]["state"] = "merged" if args[1] == "merge" else "closed"
json.dump(st, open(path, "w"))
sys.stdout.write(out)
