"""Read the composition of research funding from the published achievement workbook."""

import hashlib
import re
from datetime import datetime, timezone
from decimal import Decimal

from .workbooks import _safe_workbook, _rows, _money, _text, WorkbookImportError
from .snapshots import state, set_state

HEADERS = ["年份", "序号", "成果类型", "期刊/会议名称", "论文/成果名称", "论文类型", "资助类型",
           "发表/召开时间", "资助类别", "资助金额（元）", "地点", "状态", "备注"]
ITEM_KEYS = ["year", "kind", "venue", "title", "paper_type", "funding_type", "published_at",
             "category", "amount", "location", "note"]


def parse_research_workbook(data):
    book = _safe_workbook(data)
    try:
        if "汇总" not in book.sheetnames:
            raise WorkbookImportError("成果总表缺少“汇总”页。")
        rows = iter(_rows(book["汇总"], 30))
        headers = [_text(cell.value).strip() for cell in next(rows, ())]
        if headers.count("姓名") != 1 or headers.count("资助总额（元）") != 1:
            raise WorkbookImportError("成果总表必须包含唯一的姓名、资助总额（元）列。")
        name_col, total_col = headers.index("姓名"), headers.index("资助总额（元）")
        people = {}
        for row in rows:
            name = _text(row[name_col].value).strip()
            if not name or re.fullmatch(r"\d+人", name) or _text(row[0].value).strip() in {"合计", "总计", "小计", "总数"}:
                continue
            if name in people:
                raise WorkbookImportError("成果总表存在重复姓名，无法安全关联资助明细。")
            total = _money(row[total_col].value)
            person = {"name": name, "research_total": total, "items": [], "needs_review": total is None,
                      "detail_available": False}
            people[name] = person
            if name not in book.sheetnames:
                person["needs_review"] = True
                continue
            own = iter(_rows(book[name], 13))
            next(own, ())  # Person title on row 1; fixed field headers on row 2.
            if [cell.value for cell in next(own, ())] != HEADERS:
                raise WorkbookImportError(f"{name} 的成果明细表头无法识别，保留上一版数据。")
            footer, year = [], ""
            for row in own:
                values = [cell.value for cell in row]
                if _text(values[0]).strip() == "合计":
                    footer.append(_money(values[9]))
                    continue
                if all(value is None for value in values):
                    continue
                if footer:
                    raise WorkbookImportError("成果明细合计行后存在未识别数据。")
                year = _text(values[0]).strip() or year
                amount = _money(values[9])
                note = "；".join(_text(values[i]).strip() for i in (11, 12) if values[i] is not None)
                person["items"].append({"sequence": len(person["items"]) + 1,
                    "year": year, "kind": _text(values[2]), "venue": _text(values[3]),
                    "title": _text(values[4]), "paper_type": _text(values[5]),
                    "funding_type": _text(values[6]), "published_at": _text(values[7]),
                    "category": _text(values[8]), "amount": amount, "location": _text(values[10]),
                    "note": note, "needs_review": amount is None or "待核对" in note})
            person["detail_available"] = True
            summed = sum((Decimal(item["amount"]) for item in person["items"] if item["amount"] is not None), Decimal("0.00"))
            person["needs_review"] |= (len(footer) != 1 or footer[0] is None or
                any(item["needs_review"] for item in person["items"]) or
                (len(footer) == 1 and footer[0] != str(summed.quantize(Decimal("0.01")))) or
                total != str(summed.quantize(Decimal("0.01"))))
        if not people:
            raise WorkbookImportError("成果总表没有有效教师记录。")
        return list(people.values())
    finally:
        book.close()


def save_research(db, *, filename, data, version_id, source_key, people):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    snapshot = db.execute("INSERT INTO research_snapshots(filename,sha256,source_version_id,imported_at) VALUES(?,?,?,?)",
                          (filename, hashlib.sha256(data).hexdigest(), version_id, now)).lastrowid
    for person in people:
        db.execute("INSERT INTO research_people(snapshot_id,name,research_total,needs_review) VALUES(?,?,?,?)",
                   (snapshot, person["name"], person["research_total"], int(person["needs_review"])))
        for item in person["items"]:
            columns = ["snapshot_id", "name", "sequence", *ITEM_KEYS, "needs_review"]
            values = [snapshot, person["name"], item["sequence"], *[item[key] for key in ITEM_KEYS], int(item["needs_review"])]
            db.execute(f"INSERT INTO research_items({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", values)
    set_state(db, "research_snapshot_id", snapshot)
    set_state(db, "research_source_key", source_key)
    db.execute("INSERT INTO audit_events(actor,action,subject,created_at) VALUES(?,?,?,?)",
               ("FILEAGENT_SYNC", "RESEARCH_IMPORTED", str(snapshot), now))


def read_research(db, name, balance_research_total):
    current = state(db, "research_snapshot_id")
    if current is None:
        return {"status": "NOT_SYNCED", "items": [], "message": "资助总额明细尚未同步。"}
    person = db.execute("SELECT * FROM research_people WHERE snapshot_id=? AND name=?", (int(current), name)).fetchone()
    if person is None:
        return {"status": "NOT_FOUND", "items": [], "message": "成果总表中未找到本人的资助明细。"}
    snapshot = db.execute("SELECT filename,source_version_id,imported_at FROM research_snapshots WHERE id=?", (int(current),)).fetchone()
    items = db.execute("SELECT * FROM research_items WHERE snapshot_id=? AND name=? ORDER BY sequence", (int(current), name)).fetchall()
    matches = person["research_total"] == balance_research_total if balance_research_total is not None else None
    review = bool(person["needs_review"]) or matches is False
    return {"status": "NEEDS_REVIEW" if review else "READY",
            "research_total": person["research_total"], "matches_balance_total": matches,
            "filename": snapshot["filename"], "version_id": snapshot["source_version_id"],
            "updated_at": snapshot["imported_at"], "sync_status": state(db, "research_sync_status"),
            "items": [{"sequence": row["sequence"], **{key: row[key] for key in ITEM_KEYS},
                       "needs_review": bool(row["needs_review"])} for row in items],
            "message": "资助明细合计或余额表资助总额不一致，请核对。" if review else ""}
