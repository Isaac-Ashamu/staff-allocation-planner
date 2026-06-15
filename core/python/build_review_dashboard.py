"""Build a self-contained HTML review dashboard from recommendation outputs."""

from __future__ import annotations

import csv
import html
from pathlib import Path


OUTPUT_DIR = Path(r".\RecommenderSystem\output")
RECOMMENDATIONS = OUTPUT_DIR / "july-2026-recommendations-python.csv"
PLAN = OUTPUT_DIR / "july-2026-greedy-plan.csv"
DASHBOARD = OUTPUT_DIR / "review-dashboard.html"


def main() -> int:
    recommendations = _read_csv(RECOMMENDATIONS)
    plan = _read_csv(PLAN)
    DASHBOARD.write_text(_html(recommendations, plan), encoding="utf-8")
    print(f"Wrote {DASHBOARD}")
    return 0


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _html(recommendations: list[dict[str, str]], plan: list[dict[str, str]]) -> str:
    sessions = len({row.get("SessionId", "") for row in recommendations})
    workers = len({row.get("WorkerId", "") for row in recommendations})
    warnings = sum(1 for row in recommendations if row.get("Warnings"))
    top_rows = recommendations[:200]
    plan_rows = plan[:100]
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Booking Recommendation Review</title>
  <style>
    body {{ font-family: Segoe UI, Arial, sans-serif; margin: 0; background: #f7f7f4; color: #202124; }}
    header {{ padding: 24px 32px; background: #17324d; color: white; }}
    main {{ padding: 24px 32px; }}
    .metrics {{ display: grid; grid-template-columns: repeat(4, minmax(140px, 1fr)); gap: 12px; margin-bottom: 24px; }}
    .metric {{ background: white; border: 1px solid #ddd; border-radius: 6px; padding: 14px; }}
    .metric b {{ display: block; font-size: 24px; }}
    section {{ margin-bottom: 28px; }}
    h1, h2 {{ margin: 0 0 12px; }}
    table {{ width: 100%; border-collapse: collapse; background: white; border: 1px solid #ddd; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid #e7e7e2; text-align: left; vertical-align: top; font-size: 13px; }}
    th {{ position: sticky; top: 0; background: #eceee8; z-index: 1; }}
    .table-wrap {{ max-height: 560px; overflow: auto; border-radius: 6px; }}
    .warn {{ color: #8a4b00; }}
  </style>
</head>
<body>
  <header>
    <h1>Booking Recommendation Review</h1>
    <div>Generated from current recommendation and greedy-plan CSV outputs.</div>
  </header>
  <main>
    <div class="metrics">
      <div class="metric"><b>{len(recommendations)}</b>candidate rows</div>
      <div class="metric"><b>{sessions}</b>sessions with candidates</div>
      <div class="metric"><b>{workers}</b>candidate workers</div>
      <div class="metric"><b>{warnings}</b>rows with warnings</div>
    </div>
    <section>
      <h2>Greedy Plan</h2>
      <div class="table-wrap">{_table(plan_rows, ["BookingDate", "Start", "Finish", "ServiceUserName", "Role", "WorkerName", "Score", "Warnings"])}</div>
    </section>
    <section>
      <h2>Ranked Candidates</h2>
      <div class="table-wrap">{_table(top_rows, ["BookingDate", "Start", "Finish", "ServiceUserName", "Slot", "WorkerName", "Score", "TravelMinutes", "Reasons", "Warnings"])}</div>
    </section>
  </main>
</body>
</html>"""


def _table(rows: list[dict[str, str]], columns: list[str]) -> str:
    head = "".join(f"<th>{html.escape(col)}</th>" for col in columns)
    body = []
    for row in rows:
        cells = []
        for col in columns:
            value = row.get(col, "")
            cls = " class=\"warn\"" if col == "Warnings" and value else ""
            cells.append(f"<td{cls}>{html.escape(value)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return "<table><thead><tr>" + head + "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"


if __name__ == "__main__":
    raise SystemExit(main())

