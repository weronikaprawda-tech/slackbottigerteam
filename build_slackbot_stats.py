#!/usr/bin/env python3
"""Rebuild the Customer Zero Slackbot stats shown on the site.

WORKFLOW
--------
1. In Tableau, open the "Slackbot - Customer Zero" dashboard.
2. Download it as  Download -> Crosstab -> CSV  (the default grouping is fine:
   Bucket 1 = Mgr Lvl 2, Bucket 2 = Mgr Lvl 3, Bucket 3 = Name).
3. Save the file into DROP_DIR (below). You can keep old exports there —
   the script always picks the most recently modified source CSV.
4. Run:   python3 build_slackbot_stats.py            (updates index.html)
   or:     python3 build_slackbot_stats.py --push     (also commits + pushes)

The script:
  * auto-detects the newest raw export (ignores files it generates itself),
  * reads the UTF-16 / tab-separated Tableau crosstab,
  * drops the Grand Total row and all "Total" subtotal rows,
  * recomputes headline KPIs, adoption/message trends, the RVP leaderboard
    and the top-10 power users,
  * rewrites the `var DATA = {...};` line inside index.html,
  * writes a tidy long-format CSV + summary JSON next to the source for
    your own analysis.
"""

import csv
import io
import json
import os
import re
import sys
import glob
import subprocess
from collections import OrderedDict
from datetime import datetime

DROP_DIR = "/Users/weronika.prawda/Documents/Slackbot Usage Customer Zero"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(PROJECT_DIR, "index.html")

# Files the script itself writes into DROP_DIR — never treat these as a source.
GENERATED_PREFIX = "slackbot_"


def find_latest_csv():
    candidates = []
    for path in glob.glob(os.path.join(DROP_DIR, "*.csv")):
        if os.path.basename(path).startswith(GENERATED_PREFIX):
            continue
        candidates.append(path)
    if not candidates:
        raise SystemExit("No source CSV found in:\n  " + DROP_DIR +
                         "\nDownload the dashboard as Crosstab -> CSV into that folder first.")
    return max(candidates, key=os.path.getmtime)


def read_text(path):
    for enc in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            with open(path, encoding=enc) as f:
                return f.read()
        except (UnicodeError, UnicodeDecodeError):
            continue
    raise SystemExit("Could not decode " + path + " as UTF-16 or UTF-8.")


def parse(content):
    first_line = content.split("\n", 1)[0]
    delim = "\t" if first_line.count("\t") >= first_line.count(",") else ","
    return list(csv.reader(io.StringIO(content), delimiter=delim))


def num(x):
    x = (x or "").strip().replace(",", "").replace("%", "")
    if x == "":
        return None
    try:
        f = float(x)
        return int(f) if f == int(f) else f
    except ValueError:
        return None


def fmt_label(d):
    try:
        return datetime.strptime(d, "%m/%d/%Y").strftime("%b %-d")
    except ValueError:
        return d


def build(rows):
    if len(rows) < 4:
        raise SystemExit("Unexpected CSV: fewer than 4 rows. Is this the crosstab export?")

    metric_row = rows[1]
    date_row = rows[2]

    # Map metric blocks: columns 4+ (cols 0-3 are the dimension buckets + Employee ID).
    cols_meta = []
    for c in range(4, len(metric_row)):
        m = metric_row[c].strip()
        d = date_row[c].strip()
        if m and d:
            cols_meta.append((m, d, c))
    if not cols_meta:
        raise SystemExit("Could not find metric/date columns — did the export layout change?")

    metrics = list(OrderedDict((m, 1) for m, _, _ in cols_meta).keys())
    dates = list(OrderedDict((d, 1) for _, d, _ in cols_meta).keys())
    data = rows[3:]

    def latest_val(row, metric):
        vals = [num(row[c]) for (mm, d, c) in cols_meta if mm == metric and c < len(row)]
        return vals[-1] if vals else None

    # Grand Total series (headline trends).
    grand = next((r for r in data if r and r[0].strip() == "Grand Total"), None)
    if grand is None:
        raise SystemExit("No 'Grand Total' row found — cannot compute headline trends.")

    gt = {}
    for m in metrics:
        gt[m] = [num(grand[c]) if c < len(grand) else None for (mm, d, c) in cols_meta if mm == m]

    def gt_last(m):
        return gt.get(m, [None])[-1]

    # RVP subtotal rows (Bucket 01 leader, rest = Total).
    orgs = []
    for row in data:
        b1, b2, b3, eid = (row[0].strip(), row[1].strip(), row[2].strip(), row[3].strip())
        if b1 != "Grand Total" and b2 == "Total" and b3 == "Total" and eid == "Total":
            orgs.append({
                "org": b1,
                "users": latest_val(row, "User Count"),
                "eauPct": latest_val(row, "Slackbot EAU %"),
                "wauPct": latest_val(row, "Slackbot WAU %"),
                "mauPct": latest_val(row, "Slackbot MAU %"),
                "msg28d": latest_val(row, "Slackbot 28d Messages"),
            })
    orgs.sort(key=lambda x: (x["users"] or 0), reverse=True)

    # Individual employees (Employee ID is numeric).
    employees = []
    for row in data:
        eid = row[3].strip() if len(row) > 3 else ""
        if not eid.isdigit():
            continue
        employees.append({
            "name": row[2].strip(),
            "org": row[0].strip(),
            "msg28d": latest_val(row, "Slackbot 28d Messages") or 0,
            "msg7d": latest_val(row, "Slackbot 7d Messages") or 0,
        })
    top_users = sorted(employees, key=lambda x: x["msg28d"], reverse=True)[:10]

    # Trim the leading window where adoption metrics are still zero (pre-launch).
    eau = gt.get("Slackbot EAU %", [])
    start = 0
    for i, v in enumerate(eau):
        if v:
            start = i
            break

    labels = [fmt_label(d) for d in dates]

    def slc(m):
        return gt.get(m, [])[start:]

    compact = {
        "labels": labels[start:],
        "users": slc("User Count"),
        "eauPct": slc("Slackbot EAU %"),
        "wauPct": slc("Slackbot WAU %"),
        "mauPct": slc("Slackbot MAU %"),
        "msg28d": slc("Slackbot 28d Messages"),
        "msg7d": slc("Slackbot 7d Messages"),
        "pos": slc("Slackbot LT Positive Feedback"),
        "neg": slc("Slackbot LT Negative Feedback"),
        "inTok28d": slc("Input Tokens 28d (M, delayed)"),
        "outTok28d": slc("Output Tokens 28d (M, delayed)"),
        "latest": {
            "users": gt_last("User Count"),
            "eau": gt_last("Slackbot EAU"), "eauPct": gt_last("Slackbot EAU %"),
            "wau": gt_last("Slackbot WAU"), "wauPct": gt_last("Slackbot WAU %"),
            "mau": gt_last("Slackbot MAU"), "mauPct": gt_last("Slackbot MAU %"),
            "msg28d": gt_last("Slackbot 28d Messages"),
            "msg7d": gt_last("Slackbot 7d Messages"),
            "pos": gt_last("Slackbot LT Positive Feedback"),
            "neg": gt_last("Slackbot LT Negative Feedback"),
            "neverUsed": gt_last("Never Used Slackbot"),
        },
        "orgs": [
            {"org": o["org"], "users": o["users"], "eauPct": o["eauPct"],
             "wauPct": o["wauPct"], "msg28d": o["msg28d"]}
            for o in orgs if (o["users"] or 0) >= 5
        ],
        "topUsers": [
            {"name": u["name"], "org": u["org"], "msg28d": u["msg28d"], "msg7d": u["msg7d"]}
            for u in top_users
        ],
        "employees": len(employees),
        "asOf": dates[-1],
    }

    # Long-format tidy export (employees only) for ad-hoc analysis.
    long_rows = []
    for row in data:
        eid = row[3].strip() if len(row) > 3 else ""
        if not eid.isdigit():
            continue
        for (m, d, c) in cols_meta:
            v = num(row[c]) if c < len(row) else None
            if v is None:
                continue
            long_rows.append([row[0].strip(), row[1].strip(), row[2].strip(), eid, d, m, v])

    return compact, long_rows


def update_index(compact):
    html = read_text(INDEX_HTML)
    new_json = json.dumps(compact, ensure_ascii=False)
    pattern = r"var DATA = [^\n]*;"
    if not re.search(pattern, html):
        raise SystemExit("Could not locate the `var DATA = ...;` line in index.html.")
    html2 = re.sub(pattern, "var DATA = " + new_json + ";", html, count=1)
    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write(html2)


def write_side_outputs(compact, long_rows):
    with open(os.path.join(DROP_DIR, "slackbot_stats_embed.json"), "w", encoding="utf-8") as f:
        json.dump(compact, f, ensure_ascii=False, indent=2)
    with open(os.path.join(DROP_DIR, "slackbot_clean_long.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Mgr_Lvl2", "Mgr_Lvl3", "Name", "Employee_ID", "Date", "Metric", "Value"])
        w.writerows(long_rows)


def git_push(source_name):
    subprocess.run(["git", "-C", PROJECT_DIR, "add", "index.html"], check=True)
    msg = "Refresh Customer Zero Slackbot stats from " + source_name
    subprocess.run(["git", "-C", PROJECT_DIR, "commit", "-m", msg], check=True)
    subprocess.run(["git", "-C", PROJECT_DIR, "push", "origin", "main"], check=True)


def main():
    src = find_latest_csv()
    print("Source     :", src)
    rows = parse(read_text(src))
    compact, long_rows = build(rows)
    update_index(compact)
    write_side_outputs(compact, long_rows)

    L = compact["latest"]
    print("As of      :", compact["asOf"])
    print("Employees  :", compact["employees"])
    print("EAU / WAU  : {}% / {}%".format(L["eauPct"], L["wauPct"]))
    print("28d msgs   :", format(L["msg28d"], ","))
    print("RVPs       :", len(compact["orgs"]), " Top users:", len(compact["topUsers"]))
    print("Updated    :", INDEX_HTML)

    if "--push" in sys.argv:
        git_push(os.path.basename(src))
        print("Pushed to origin/main — Cloudflare will redeploy.")
    else:
        print("\nReview locally, then commit/push (or re-run with --push).")


if __name__ == "__main__":
    main()
