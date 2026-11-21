#!/usr/bin/env python3
"""Build a self-contained HTML report of triage verdicts from output/*.jsonl."""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from collections import Counter
from pathlib import Path


INSTITUTIONS = ("bank", "emoney", "investment", "payments_fx")
VERDICTS = ("CLOSE", "RFI", "ESCALATE")


def load_cases(output_dir: Path) -> list[dict]:
    cases: list[dict] = []
    missing: list[str] = []
    for name in INSTITUTIONS:
        path = output_dir / f"{name}.jsonl"
        if not path.is_file():
            missing.append(str(path))
            continue
        with path.open(encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"Invalid JSON in {path}:{line_no}: {exc}") from exc
                cases.append(
                    {
                        "case_id": row.get("case_id", ""),
                        "institution": name,
                        "verdict": row.get("verdict", ""),
                        "reason_code": row.get("reason_code", ""),
                        "reason": row.get("reason", ""),
                        "evidence": row.get("evidence") or [],
                    }
                )
    if missing:
        raise SystemExit(
            "Missing triage outputs (run system/run first):\n  " + "\n  ".join(missing)
        )
    return cases


def short_reason(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def count_block(cases: list[dict]) -> dict:
    overall = Counter(c["verdict"] for c in cases)
    by_inst: dict[str, Counter] = {name: Counter() for name in INSTITUTIONS}
    for c in cases:
        by_inst[c["institution"]][c["verdict"]] += 1
    return {
        "total": len(cases),
        "overall": {v: overall.get(v, 0) for v in VERDICTS},
        "by_institution": {
            name: {v: by_inst[name].get(v, 0) for v in VERDICTS} for name in INSTITUTIONS
        },
    }


def render_html(cases: list[dict]) -> str:
    counts = count_block(cases)
    # Embed compact JSON for the client filter table.
    payload = []
    for c in cases:
        payload.append(
            [
                c["case_id"],
                c["institution"],
                c["verdict"],
                c["reason_code"],
                short_reason(c["reason"]),
                ", ".join(c["evidence"]),
            ]
        )
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    counts_json = json.dumps(counts, ensure_ascii=False, separators=(",", ":"))

    overall = counts["overall"]
    summary_bits = " · ".join(f"{v}: {overall[v]}" for v in VERDICTS)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Case verdicts — triage scan</title>
<style>
  :root {{
    --bg: #f6f4ef;
    --ink: #1c1b19;
    --muted: #5c584f;
    --line: #d9d3c6;
    --panel: #fffdf8;
    --close: #1f6b45;
    --close-bg: #e5f3eb;
    --rfi: #8a5a12;
    --rfi-bg: #f8eed9;
    --esc: #8b1e2d;
    --esc-bg: #f8e4e7;
    --active: #1c1b19;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font: 15px/1.45 "IBM Plex Sans", "Segoe UI", sans-serif;
    color: var(--ink);
    background:
      radial-gradient(ellipse at top left, #efe8d8 0%, transparent 55%),
      linear-gradient(180deg, #f6f4ef 0%, #ebe6db 100%);
    min-height: 100vh;
  }}
  header {{
    padding: 1.5rem 1.25rem 1rem;
    border-bottom: 1px solid var(--line);
    background: color-mix(in srgb, var(--panel) 88%, transparent);
    backdrop-filter: blur(6px);
    position: sticky;
    top: 0;
    z-index: 2;
  }}
  h1 {{
    margin: 0 0 0.25rem;
    font: 600 1.35rem/1.2 "IBM Plex Sans", sans-serif;
    letter-spacing: -0.02em;
  }}
  .meta {{ color: var(--muted); font-size: 0.92rem; }}
  .filters {{
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem 1.25rem;
    padding: 0.85rem 1.25rem;
    align-items: center;
  }}
  .filter-group {{
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    align-items: center;
  }}
  .filter-group span.label {{
    color: var(--muted);
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-right: 0.25rem;
  }}
  button {{
    appearance: none;
    border: 1px solid var(--line);
    background: var(--panel);
    color: var(--ink);
    border-radius: 6px;
    padding: 0.35rem 0.7rem;
    font: inherit;
    cursor: pointer;
  }}
  button:hover {{ border-color: #b7b09f; }}
  button.active {{
    background: var(--active);
    color: #fff;
    border-color: var(--active);
  }}
  input[type="search"] {{
    min-width: 14rem;
    padding: 0.4rem 0.65rem;
    border: 1px solid var(--line);
    border-radius: 6px;
    background: var(--panel);
    font: inherit;
  }}
  .stats {{
    padding: 0 1.25rem 0.75rem;
    color: var(--muted);
    font-size: 0.92rem;
  }}
  .stats strong {{ color: var(--ink); font-weight: 600; }}
  main {{ padding: 0 1.25rem 2rem; overflow-x: auto; }}
  table {{
    width: 100%;
    border-collapse: collapse;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
    overflow: hidden;
  }}
  th, td {{
    text-align: left;
    padding: 0.55rem 0.7rem;
    border-bottom: 1px solid var(--line);
    vertical-align: top;
  }}
  th {{
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--muted);
    background: #f3efe6;
    position: sticky;
    top: 0;
  }}
  tr:last-child td {{ border-bottom: 0; }}
  tbody tr:hover {{ background: #faf7f0; }}
  .verdict {{
    display: inline-block;
    font-size: 0.78rem;
    font-weight: 600;
    letter-spacing: 0.03em;
    padding: 0.15rem 0.45rem;
    border-radius: 4px;
  }}
  .verdict.CLOSE {{ color: var(--close); background: var(--close-bg); }}
  .verdict.RFI {{ color: var(--rfi); background: var(--rfi-bg); }}
  .verdict.ESCALATE {{ color: var(--esc); background: var(--esc-bg); }}
  .mono {{ font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 0.88rem; }}
  .reason {{ max-width: 36rem; color: var(--muted); }}
  .evidence {{ max-width: 18rem; color: var(--muted); font-size: 0.88rem; }}
  .empty {{
    padding: 2rem;
    text-align: center;
    color: var(--muted);
  }}
  @media (max-width: 720px) {{
    input[type="search"] {{ min-width: 100%; width: 100%; }}
    .reason, .evidence {{ max-width: 14rem; }}
  }}
</style>
</head>
<body>
<header>
  <h1>Case verdicts</h1>
  <p class="meta">Triage scan across bank, emoney, investment, payments_fx — {counts["total"]} cases ({html.escape(summary_bits)})</p>
</header>
<div class="filters">
  <div class="filter-group" id="inst-filters" role="group" aria-label="Institution">
    <span class="label">Institution</span>
  </div>
  <div class="filter-group" id="verdict-filters" role="group" aria-label="Verdict">
    <span class="label">Verdict</span>
  </div>
  <div class="filter-group">
    <span class="label">Search</span>
    <input type="search" id="q" placeholder="case id, reason code, evidence…" autocomplete="off">
  </div>
</div>
<p class="stats" id="stats"></p>
<main>
  <table>
    <thead>
      <tr>
        <th>Case</th>
        <th>Institution</th>
        <th>Verdict</th>
        <th>Reason code</th>
        <th>Reason</th>
        <th>Evidence</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  <p class="empty" id="empty" hidden>No cases match these filters.</p>
</main>
<script>
const DATA = {data_json};
const COUNTS = {counts_json};
const INSTITUTIONS = {json.dumps(list(INSTITUTIONS))};
const VERDICTS = {json.dumps(list(VERDICTS))};

let inst = "ALL";
let verdict = "ALL";

function el(tag, attrs, text) {{
  const node = document.createElement(tag);
  if (attrs) Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
  if (text != null) node.textContent = text;
  return node;
}}

function buildButtons(containerId, values, allLabel, onPick) {{
  const root = document.getElementById(containerId);
  const all = el("button", {{ type: "button", "data-value": "ALL", class: "active" }}, allLabel);
  all.addEventListener("click", () => onPick("ALL", root));
  root.appendChild(all);
  values.forEach(v => {{
    const b = el("button", {{ type: "button", "data-value": v }}, v);
    b.addEventListener("click", () => onPick(v, root));
    root.appendChild(b);
  }});
}}

function setActive(root, value) {{
  root.querySelectorAll("button").forEach(b => {{
    b.classList.toggle("active", b.dataset.value === value);
  }});
}}

function matches(row, query) {{
  if (inst !== "ALL" && row[1] !== inst) return false;
  if (verdict !== "ALL" && row[2] !== verdict) return false;
  if (!query) return true;
  const hay = row.join(" ").toLowerCase();
  return hay.includes(query);
}}

function render() {{
  const q = (document.getElementById("q").value || "").trim().toLowerCase();
  const tbody = document.getElementById("rows");
  const empty = document.getElementById("empty");
  tbody.replaceChildren();
  let shown = 0;
  const byVerdict = {{ CLOSE: 0, RFI: 0, ESCALATE: 0 }};
  for (const row of DATA) {{
    if (!matches(row, q)) continue;
    shown += 1;
    byVerdict[row[2]] = (byVerdict[row[2]] || 0) + 1;
    const tr = document.createElement("tr");
    tr.appendChild(el("td", {{ class: "mono" }}, row[0]));
    tr.appendChild(el("td", null, row[1]));
    const vd = el("td");
    vd.appendChild(el("span", {{ class: "verdict " + row[2] }}, row[2]));
    tr.appendChild(vd);
    tr.appendChild(el("td", {{ class: "mono" }}, row[3]));
    tr.appendChild(el("td", {{ class: "reason" }}, row[4]));
    tr.appendChild(el("td", {{ class: "evidence mono" }}, row[5]));
    tbody.appendChild(tr);
  }}
  empty.hidden = shown > 0;
  document.querySelector("table").hidden = shown === 0;
  const parts = VERDICTS.map(v => v + ": " + (byVerdict[v] || 0));
  document.getElementById("stats").innerHTML =
    "Showing <strong>" + shown + "</strong> of " + COUNTS.total +
    " — " + parts.join(" · ");
}}

buildButtons("inst-filters", INSTITUTIONS, "All", (v, root) => {{
  inst = v; setActive(root, v); render();
}});
buildButtons("verdict-filters", VERDICTS, "All", (v, root) => {{
  verdict = v; setActive(root, v); render();
}});
document.getElementById("q").addEventListener("input", render);
render();
</script>
</body>
</html>
"""


def main(argv: list[str]) -> int:
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=repo / "output",
        help="Directory containing institution *.jsonl files",
    )
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        action="append",
        dest="outs",
        help="HTML output path (repeatable). Default: output/case-verdicts.html",
    )
    args = parser.parse_args(argv)

    cases = load_cases(args.output_dir)
    html_text = render_html(cases)
    outs = args.outs or [repo / "output" / "case-verdicts.html"]
    for out in outs:
        out = out.resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html_text, encoding="utf-8")
        print(f"Wrote {len(cases)} cases → {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
