#!/usr/bin/env bash
# QA -> fix -> QA on the CMIP7 golden file, every step recorded with etb run. Works offline.
#
# The golden file has findings esgf-fix-tool can fix (e.g. C001: 'history' on the data variable) and
# findings it cannot (repacking, review rules). The test checks that after one fix iteration every
# finding with an implemented fix is gone, and that no new finding appeared.
#
# Expects golden files mounted at /data (read-only is fine: the test works on a copy).
#   docker run --rm -v $PWD/tests:/tests -v $PWD/tests/golden:/data:ro esgf-toolbox bash /tests/run_qa_fix_loop.sh

set -euo pipefail

DATA=${DATA:-/data}
work=$(mktemp -d)
cd "$work"
export ETB_LOG="$work/etb-runs.jsonl"
mkdir staging
cp "$DATA"/CMIP7/*.nc staging/

qa() {  # qa NOTE OUTDIR -> prints the full report path
    etb run --note "$1" -- esgqa -P 1 -t wcrp_cmip7:1.0 -t cf:1.11 -o "$work/$2" "$work/staging" \
        > "$work/$2.log" 2>&1
    ls "$work/$2"/qa_result_*.json | grep -v '\.cluster\.json$'
}

classify() {  # classify REPORT OUTPUT [--previous REPORT]: the exit code is the loop decision, not a failure
    local report=$1 output=$2; shift 2
    etb run -- esgf-fix-tool classify "$report" "$@" --json > "$output" || true
}

echo "== QA before fixes"
before=$(qa "QA before fixes" qa_before)
classify "$before" classify_before.json

echo "== plan + apply"
etb run -- esgf-fix-tool plan "$before" -o fixplan.json
etb run --note "fix iteration 1" -- esgf-fix-tool apply fixplan.json --only-under "$work/staging" \
    --fixlog fixlog.jsonl --quiet

echo "== QA after fixes"
after=$(qa "QA after fixes" qa_after)
classify "$after" classify_after.json --previous "$before"

python3 - <<'EOF'
import json, sys

before = json.load(open("classify_before.json"))["counts"]
after = json.load(open("classify_after.json"))["counts"]
fixlog = [json.loads(line) for line in open("fixlog.jsonl")]
runs = [json.loads(line) for line in open("etb-runs.jsonl")]

print(f"{'severity':>8}  {'outcome':12} {'before':>6} {'after':>6}")
problems = []
for severity in sorted(set(before) | set(after), reverse=True):
    b, a = before.get(severity, {}), after.get(severity, {})
    for outcome in sorted(set(b) | set(a)):
        nb, na = b.get(outcome, 0), a.get(outcome, 0)
        print(f"{severity:>8}  {outcome:12} {nb:6} {na:6}")
        if outcome == "fix" and na:
            problems.append(f"severity {severity}: {na} fixable finding(s) still reported after apply")
        elif na > nb:
            problems.append(f"severity {severity}: {na - nb} new '{outcome}' finding(s) after apply")

if not any(b.get("fix") for b in before.values()):
    problems.append("the golden file has no fixable finding any more: the test checks nothing")
if not fixlog or not all(any(s["changed"] for s in rec["steps"]) for rec in fixlog):
    problems.append("the fixlog does not record a changed file")
if fixlog and not (fixlog[0]["tool_version"] and fixlog[0]["rules_digest"]):
    problems.append("the fixlog lacks the tool version or rules digest")
ends = [r for r in runs if r["event"] == "end"]
if len(ends) != len([r for r in runs if r["event"] == "start"]) or len(ends) != 6:
    problems.append(f"expected 6 complete etb runs, found {len(ends)} end records")

print(f"\nfixlog: {len(fixlog)} file(s), esgf-fix-tool {fixlog[0]['tool_version'] if fixlog else '?'}, "
      f"{sum(len(r['steps']) for r in fixlog)} step(s)")
if problems:
    print("\nFAILED:\n  " + "\n  ".join(problems))
    sys.exit(1)
print("QA -> fix -> QA passed: every fixable finding is gone, nothing new appeared.")
EOF

echo
etb log
