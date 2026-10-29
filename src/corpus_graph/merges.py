"""`corpus-graph merges list|show|approve|reject`: review proposed merges.

Decisions are written to canon/merges.yaml (durable, human-editable) and mirrored
into work/proposed_merges.json. They take effect on the next `resolve`.
"""

from __future__ import annotations

from .canon import Canon
from .schema import Schema
from .util import Workspace, eprint, read_json, write_json


def _load(ws: Workspace) -> dict:
    data = read_json(ws.proposed_merges)
    if not data:
        eprint("no proposals yet: run `corpus-graph resolve` first")
        return {"proposals": []}
    return data


def list_(ws: Workspace, status: str = "pending", ntype: str | None = None) -> int:
    data = _load(ws)
    rows = [p for p in data["proposals"] if (status == "all" or p["status"] == status) and (not ntype or p["type"] == ntype)]
    if not rows:
        print(f"No {status} proposals.")
        return 0
    for p in rows:
        llm = f"  llm: {p['llm']['verdict']} ({p['llm']['reason']})" if p.get("llm") else ""
        print(f"{p['id']}  [{p['status']}] {p['kind']} {p['type']}  score {p['score']}{llm}")
        if p.get("reason"):
            print(f"    {p['reason']}")
        for m in p["members"]:
            star = "→" if m == p.get("into") else " "
            name = (p.get("member_names") or {}).get(m)
            docs = ", ".join((p.get("member_docs") or {}).get(m, []))
            print(f"   {star} {m}  {name!r}  docs: {docs}")
    print(f"\n{len(rows)} proposal(s). Approve with `corpus-graph merges approve <id> [--into <node id>]`, "
          f"reject with `corpus-graph merges reject <id>`, then re-run `resolve` and `build`.")
    return 0


def decide(ws: Workspace, ids: list[str], approve: bool, into: str | None = None,
           min_score: float | None = None, ntype: str | None = None) -> int:
    data = _load(ws)
    canon = Canon(ws.canon, Schema.load(ws.canon))
    targets = []
    for p in data["proposals"]:
        if p["status"] != "pending":
            continue
        if ids and not any(p["id"].startswith(i) for i in ids):
            continue
        if min_score is not None and (p["kind"] != "fuzzy" or p["score"] < min_score or (ntype and p["type"] != ntype)):
            continue
        targets.append(p)
    if not targets:
        eprint("no matching pending proposals")
        return 1
    for p in targets:
        if approve:
            target = into or p.get("into")
            if p["kind"] == "ambiguous" and not into:
                eprint(f"{p['id']}: ambiguous proposal; choose the target with --into (one of {', '.join(p['members'][1:])})")
                continue
            if target not in p["members"]:
                eprint(f"{p['id']}: --into must be one of {', '.join(p['members'])}")
                continue
            members = p["members"] if p["kind"] == "fuzzy" else [p["members"][0], target]
            canon.approved.append({"type": p["type"], "members": members, "into": target})
            p["status"], p["into"] = "approved", target
        else:
            canon.rejected.append({"type": p["type"], "members": p["members"]})
            p["status"] = "rejected"
        print(f"{p['id']}: {p['status']}")
    canon.save_merges()
    write_json(ws.proposed_merges, data)
    print("Recorded in canon/merges.yaml. Re-run `corpus-graph resolve && corpus-graph build` to apply.")
    return 0
