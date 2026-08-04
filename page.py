#!/usr/bin/env python3
"""Generate an HTML report from structured LazyAdFinder verdicts.

``page.py`` presents results; it never evaluates testcase answers.  Evaluators
write ``verdicts.json`` beside their evidence using the shape returned by
``Verdict.to_dict()``.  This tool discovers those files, validates the three
status values and renders one static report.

Examples:
    python3 page.py
    python3 page.py --evidence evidence /path/to/more/evidence
    python3 page.py --out report.html
"""

import argparse
import html
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from verdict import Status


VERDICTS_FILE = "verdicts.json"
METADATA_FILE = "metadata.json"
VALID_STATUSES = {status.value for status in Status}
STATUS_ORDER = (Status.FAILED.value, Status.BLOCKED.value, Status.PASS.value)


class ReportError(RuntimeError):
    pass


def _load_json(path):
    try:
        return json.loads(path.read_text())
    except OSError as exc:
        raise ReportError(f"Cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReportError(f"Invalid JSON in {path}: {exc}") from exc


def _verdict_rows(document, path):
    if isinstance(document, list):
        rows = document
    elif isinstance(document, dict) and isinstance(document.get("verdicts"), list):
        rows = document["verdicts"]
    else:
        raise ReportError(f"{path} must contain a list or {{\"verdicts\": [...]}}")

    normalized = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ReportError(f"{path}: verdict #{index + 1} must be an object")
        tc = row.get("tc")
        status = row.get("status")
        reason = row.get("reason", "")
        if not isinstance(tc, str) or not tc.strip():
            raise ReportError(f"{path}: verdict #{index + 1} has no TC id")
        if status not in VALID_STATUSES:
            raise ReportError(
                f"{path}: {tc} has invalid status {status!r}; expected {sorted(VALID_STATUSES)}"
            )
        if not isinstance(reason, str):
            raise ReportError(f"{path}: {tc} reason must be a string")
        if status == Status.BLOCKED.value and not reason.strip():
            raise ReportError(f"{path}: BLOCKED verdict {tc} requires a reason")
        evidence = row.get("evidence")
        if status == Status.BLOCKED.value and any(
            row.get(key) is not None for key in ("expected", "actual", "evidence")
        ):
            raise ReportError(f"{path}: BLOCKED verdict {tc} cannot claim an evaluated answer")
        if status != Status.BLOCKED.value and (
            not isinstance(evidence, str) or not evidence.strip()
        ):
            raise ReportError(f"{path}: evaluated verdict {tc} requires evidence")

        normalized.append({
            "tc": tc,
            "status": status,
            "reason": reason,
            "expected": row.get("expected"),
            "actual": row.get("actual"),
            "evidence": evidence,
            "source": path,
        })
    return normalized


def discover(evidence_dirs):
    verdicts = []
    captures = []
    verdict_files = []
    seen = set()

    for root_value in evidence_dirs:
        root = Path(root_value).expanduser().resolve()
        if not root.exists():
            continue
        for metadata in sorted(root.rglob(METADATA_FILE)):
            captures.append(metadata.parent)
        for path in sorted(root.rglob(VERDICTS_FILE)):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            verdict_files.append(resolved)
            verdicts.extend(_verdict_rows(_load_json(resolved), resolved))
    return verdicts, captures, verdict_files


def _display(value):
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _evidence_link(row):
    reference = row.get("evidence")
    if not reference:
        return "—"
    path = Path(reference)
    if not path.is_absolute():
        path = row["source"].parent / path
    label = html.escape(reference)
    if path.exists():
        return f'<a href="{html.escape(path.resolve().as_uri(), quote=True)}">{label}</a>'
    return f'<span class="missing" title="Evidence path does not exist">{label}</span>'


def render(verdicts, captures, verdict_files, evidence_dirs):
    counts = Counter(row["status"] for row in verdicts)
    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    roots = "、".join(html.escape(str(Path(root).expanduser())) for root in evidence_dirs)

    if verdicts:
        ordered = sorted(
            verdicts,
            key=lambda row: (STATUS_ORDER.index(row["status"]), row["tc"], str(row["source"])),
        )
        rows_html = []
        for row in ordered:
            status = row["status"]
            rows_html.append(
                "<tr>"
                f'<td class="tc">{html.escape(row["tc"])}</td>'
                f'<td><span class="badge {status.lower()}">{status}</span></td>'
                f'<td class="value">{html.escape(_display(row["expected"]))}</td>'
                f'<td class="value">{html.escape(_display(row["actual"]))}</td>'
                f'<td>{html.escape(row["reason"] or "—")}</td>'
                f'<td>{_evidence_link(row)}</td>'
                f'<td class="source">{html.escape(str(row["source"]))}</td>'
                "</tr>"
            )
        body = (
            '<div class="table-wrap"><table><thead><tr>'
            '<th>TC</th><th>Status</th><th>Expected</th><th>Actual</th>'
            '<th>Reason</th><th>Evidence</th><th>Source</th>'
            '</tr></thead><tbody>' + "".join(rows_html) + "</tbody></table></div>"
        )
    else:
        body = (
            '<section class="empty"><h2>尚無 TC 判定</h2>'
            '<p>目前只有 raw evidence，或尚未建立任何 <code>verdicts.json</code>。'
            '<br>此頁不會把未執行的 TC 自動算成 BLOCKED，也不會產生假的通過數字。</p></section>'
        )

    return f'''<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LazyAdFinder2 · QA Report</title>
<style>
:root{{--bg:#f5f7f8;--panel:#fff;--ink:#172126;--muted:#66757d;--line:#dbe2e6;
--pass:#147447;--pass-bg:#e3f4ea;--failed:#b42318;--failed-bg:#fee9e7;
--blocked:#805b10;--blocked-bg:#fff1cc}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:1500px;margin:0 auto;padding:32px 24px 56px}}
h1{{margin:0;font-size:28px}}.subtitle{{color:var(--muted);margin:4px 0 24px}}
.summary{{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr));gap:12px;margin-bottom:24px}}
.tile{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}}
.tile .number{{display:block;font-size:28px;font-weight:750}}.tile .label{{color:var(--muted)}}
.table-wrap,.empty{{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:auto}}
table{{border-collapse:collapse;width:100%;min-width:1080px}}th,td{{padding:11px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
th{{position:sticky;top:0;background:#eef2f4;font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.tc{{font-weight:700;white-space:nowrap}}.value{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;max-width:280px;overflow-wrap:anywhere}}
.source{{font-size:11px;color:var(--muted);max-width:260px;overflow-wrap:anywhere}}
.badge{{display:inline-block;border-radius:999px;padding:3px 9px;font-size:12px;font-weight:750}}
.pass{{color:var(--pass);background:var(--pass-bg)}}.failed{{color:var(--failed);background:var(--failed-bg)}}
.blocked{{color:var(--blocked);background:var(--blocked-bg)}}.missing{{color:var(--failed);text-decoration:line-through}}
.empty{{padding:44px;text-align:center}}.empty h2{{margin:0 0 8px}}code{{background:#edf1f3;padding:2px 5px;border-radius:4px}}
.meta{{color:var(--muted);font-size:12px;margin-top:18px;overflow-wrap:anywhere}}
@media(max-width:700px){{main{{padding:20px 12px}}.summary{{grid-template-columns:repeat(2,1fr)}}}}
</style>
</head>
<body><main>
<h1>LazyAdFinder2 QA Report</h1>
<p class="subtitle">Page 只呈現 Verdict，不重新判定答案。</p>
<section class="summary">
<div class="tile"><span class="number">{len(verdicts)}</span><span class="label">Total verdicts</span></div>
<div class="tile"><span class="number">{counts[Status.PASS.value]}</span><span class="label">PASS</span></div>
<div class="tile"><span class="number">{counts[Status.FAILED.value]}</span><span class="label">FAILED</span></div>
<div class="tile"><span class="number">{counts[Status.BLOCKED.value]}</span><span class="label">BLOCKED</span></div>
</section>
{body}
<p class="meta">Raw captures: {len(captures)} · Verdict files: {len(verdict_files)} · Generated: {html.escape(generated)}<br>Evidence roots: {roots or '—'}</p>
</main></body></html>'''


def write_report(output, content):
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    os.replace(temporary, output)
    return output


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence",
        nargs="+",
        default=[str(Path(__file__).parent / "evidence")],
        help="one or more evidence roots",
    )
    parser.add_argument("--out", default="report.html", help="output HTML path")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    verdicts, captures, verdict_files = discover(args.evidence)
    output = write_report(
        args.out,
        render(verdicts, captures, verdict_files, args.evidence),
    )
    print(
        f"[report] {output} · verdicts={len(verdicts)} "
        f"captures={len(captures)} sources={len(verdict_files)}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReportError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        raise SystemExit(1)
