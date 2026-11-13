#!/usr/bin/env bash
# The operator workflow, end to end, on a copy of the CMIP7 golden file (docs: README "Workflow test").
# Every step goes through etb, as an operator would; each one is checked. Works offline.
#
#   copy (twice) -> deduplicate (dry run) -> QA -> fix -> QA -> repack -> DRS by hard link -> mapfiles
#   -> milestones and etb todo -> new version with --break-hardlinks -> rm the source
#
# Expects golden files mounted at /data (read-only is fine: the test works on copies).
#   docker run --rm -v $PWD/tests:/tests -v $PWD/tests/golden:/data:ro esgf-toolbox bash /tests/run_workflow.sh

set -uo pipefail

DATA=${DATA:-/data}
ROOT=$(mktemp -d)
export ETB_LOG=$ROOT/work/etb-runs.jsonl
SIMU=SIM01
SRC=$ROOT/src/$SIMU
WORK=$ROOT/work/$SIMU
RUN=$ROOT/work/_runs/$SIMU
DRS_ROOT=$ROOT/drs
QA_TESTS=(-t wcrp_cmip7:1.0 -t cf:1.11)
VERSION=20261113
mkdir -p "$SRC/ATM/Output" "$RUN" "$DRS_ROOT"
cp "$DATA"/CMIP7/*.nc "$SRC/ATM/Output/"
cd "$ROOT"

step() { echo; echo "== $*"; }
pass() { echo "   ok: $*"; }
fail() { echo "   FAILED: $*"; exit 1; }
check() { local what=$1; shift; if "$@"; then pass "$what"; else fail "$what"; fi; }
report() { ls "$RUN/$1"/qa_result_*[0-9a-f].json; }   # the full report, not the .cluster.json
milestone() { python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d['$1'])"; }
in_two_months() {  # today + 2 months, clamped to the end of the month like etb (not GNU date, which overflows)
    python3 -c "import calendar, datetime as d; t = d.datetime.now(d.UTC).date(); m = t.month + 1; y = t.year + m // 12; m = m % 12 + 1
print(t.replace(year=y, month=m, day=min(t.day, calendar.monthrange(y, m)[1])))"; }

step "0. nothing recorded yet"
check "etb todo says where to start" grep -q "start with: etb copy" <(etb todo)

step "1. copy SRC -> WORK, twice"
etb copy "$SRC" "$WORK" > copy1.out || fail "etb copy"
etb copy "$SRC" "$WORK" > copy2.out 2>&1 || fail "etb copy, second run"
check "the copy is identical" cmp -s "$SRC"/ATM/Output/*.nc "$WORK"/ATM/Output/*.nc
check "the second run says it was already copied" grep -q "was already copied on" copy2.out
check "the second run copies nothing" grep -q "copied 0 file(s), 1 already there" copy2.out

step "2. deduplicate ATM/SRF (dry run)"
etb run -- esgf-fix-tool deduplicate "$WORK" --only-under "$WORK" --dry-run \
    --fixlog "$RUN/deduplicate.preview.jsonl" > dedup.out || fail "deduplicate --dry-run"
check "nothing to delete in the golden file" grep -q "'would_delete': 0" dedup.out

step "3-4. QA -> fix -> QA"
etb run -- esgqa "${QA_TESTS[@]}" -o "$RUN/qa_1" "$WORK" > qa_1.log 2>&1 || fail "esgqa (1)"
etb run -- esgf-fix-tool classify "$(report qa_1)" --json > classify_1.json
etb run -- esgf-fix-tool plan "$(report qa_1)" -o "$RUN/fixplan_1.json" > /dev/null || fail "plan"
etb run --note "fix iteration 1" -- esgf-fix-tool apply "$RUN/fixplan_1.json" --only-under "$WORK" \
    --fixlog "$RUN/fixlog.jsonl" --quiet > /dev/null || fail "apply"
etb run -- esgqa "${QA_TESTS[@]}" -o "$RUN/qa_2" "$WORK" > qa_2.log 2>&1 || fail "esgqa (2)"
etb run -- esgf-fix-tool classify "$(report qa_2)" --previous "$(report qa_1)" --json > classify_2.json
python3 - "$RUN/fixlog.jsonl" "$ETB_LOG" <<'EOF' || fail "QA -> fix -> QA (see above)"
import json, sys
before = json.load(open("classify_1.json"))["counts"]
after = json.load(open("classify_2.json"))["counts"]
fixlog = [json.loads(line) for line in open(sys.argv[1])]
runs = [json.loads(line) for line in open(sys.argv[2])]
print(f"   {'severity':>8}  {'outcome':12} {'before':>6} {'after':>6}")
problems = []
for severity in sorted(set(before) | set(after), reverse=True):
    b, a = before.get(severity, {}), after.get(severity, {})
    for outcome in sorted(set(b) | set(a)):
        nb, na = b.get(outcome, 0), a.get(outcome, 0)
        print(f"   {severity:>8}  {outcome:12} {nb:6} {na:6}")
        if outcome == "fix" and na:
            problems.append(f"severity {severity}: {na} fixable finding(s) still reported after apply")
        elif na > nb:
            problems.append(f"severity {severity}: {na - nb} new '{outcome}' finding(s) after apply")
if not any(b.get("fix") for b in before.values()):
    problems.append("the golden file has no fixable finding any more: the test checks nothing")
if not fixlog or not any(s["changed"] for s in fixlog[0]["steps"]):
    problems.append("the fixlog does not record a changed file")
elif not (fixlog[0]["tool_version"] and fixlog[0]["rules_digest"]):
    problems.append("the fixlog lacks the tool version or the rules digest")
apply_runs = {r["run_id"] for r in runs if r.get("event") == "start" and r["argv"][1:2] == ["apply"]}
if fixlog and fixlog[0].get("etb_run_id") not in apply_runs:
    problems.append(f"fixlog etb_run_id {fixlog[0].get('etb_run_id')} is not the apply run in the etb log")
for p in problems:
    print("   FAILED:", p)
sys.exit(1 if problems else 0)
EOF
pass "every fixable finding is gone, nothing new appeared"
pass "the fixlog records the tool version, the rules digest and the etb run of apply"

step "5. repack"
etb run -- sh -c 'find "$1" -name "*.nc" -print0 | xargs -0 cmip7repack -o' _ "$WORK" > repack.log 2>&1 \
    || fail "cmip7repack"
check "files repacked in place (no *_cmip7repack copy left)" test -z "$(find "$WORK" -name '*_cmip7repack*')"
etb done "$SIMU" fixed > /dev/null || fail "etb done fixed"

step "6. into the DRS by hard link"
etb run -- esgdrs make upgrade -p cmip7 --root "$DRS_ROOT" --version "$VERSION" --link "$WORK" > esgdrs.log 2>&1 \
    || fail "esgdrs make upgrade --link"
WORK_FILE=$(find "$WORK" -name '*.nc')
DRS_FILE=$(find "$DRS_ROOT" -path "*/v$VERSION/*.nc")
check "v$VERSION/ holds a symlink to files/d$VERSION/" bash -c "readlink '$DRS_FILE' | grep -q '^../files/d$VERSION/'"
check "latest points to v$VERSION" test "$(readlink "$(dirname "$(dirname "$DRS_FILE")")/latest")" = "v$VERSION"
check "the DRS file is the WORK file (2 links, same inode)" \
    bash -c "[ \$(stat -L -c %h '$DRS_FILE') = 2 ] && [ '$DRS_FILE' -ef '$WORK_FILE' ]"
check "the source is untouched (its own file)" bash -c "! [ '$SRC'/ATM/Output/*.nc -ef '$WORK_FILE' ]"
etb done "$SIMU" linked --note "v$VERSION" > /dev/null || fail "etb done linked"

step "7. mapfiles and publication"
etb run -- esgmapfile make -p cmip7 --directory "$DRS_ROOT/MIP-DRS7" --outdir "$RUN/mapfiles" \
    --version "$VERSION" > esgmapfile.log 2>&1 || fail "esgmapfile"
check "one mapfile for version $VERSION" test "$(ls "$RUN"/mapfiles/*.v$VERSION.map | wc -l)" = 1
etb done "$SIMU" published > /dev/null || fail "etb done published"
TODO=$(etb todo "$SIMU" --json)
check "etb todo: published, source removable 2 months later" bash -c \
    "[ '$(echo "$TODO" | milestone milestone)' = published ] && [ '$(echo "$TODO" | milestone src_removable_from)' = '$(in_two_months)' ]"

step "new version: fix WORK without touching the published file"
python3 - "$WORK_FILE" "$RUN/fixplan_v2.json" <<'EOF'
import json, sys
step = {"rule": "C001", "fix": "set_var_attr", "params": {"var": "ta", "attr": "comment", "value": "new version"}}
json.dump({"rules_version": "test", "report": "test", "files": {sys.argv[1]: [step]}}, open(sys.argv[2], "w"))
EOF
comment() { python3 -c "import netCDF4,sys; print(getattr(netCDF4.Dataset(sys.argv[1])['ta'], 'comment', None))" "$1"; }
PUBLISHED=$(comment "$DRS_FILE")
etb run -- esgf-fix-tool apply "$RUN/fixplan_v2.json" --only-under "$WORK" --fixlog "$RUN/fixlog.jsonl" \
    --quiet > refused.log 2>&1
check "without --break-hardlinks: refused, nothing changed" \
    bash -c "grep -q 'Use --break-hardlinks' refused.log && [ \$(stat -L -c %h '$DRS_FILE') = 2 ]"
etb run -- esgf-fix-tool apply "$RUN/fixplan_v2.json" --only-under "$WORK" --fixlog "$RUN/fixlog.jsonl" \
    --break-hardlinks --quiet > /dev/null || fail "apply --break-hardlinks"
check "WORK has the fix" test "$(comment "$WORK_FILE")" = "new version"
check "the published DRS file is unchanged" test "$(comment "$DRS_FILE")" = "$PUBLISHED"
check "each has its own file now" bash -c "[ \$(stat -c %h '$WORK_FILE') = 1 ] && [ \$(stat -L -c %h '$DRS_FILE') = 1 ]"
check "the fixlog records hardlinks_broken" bash -c \
    "tail -1 '$RUN/fixlog.jsonl' | grep -q '\"hardlinks_broken\": 1'"

step "8. remove the source"
rm -r "$SRC"
etb done "$SIMU" src-removed > /dev/null || fail "etb done src-removed"
check "etb todo: done" grep -q "nothing: done" <(etb todo "$SIMU")
check "every etb run has its end record" python3 -c "
import json; r = [json.loads(l) for l in open('$ETB_LOG')]
s = {x['run_id'] for x in r if x['event'] == 'start'}; e = {x['run_id'] for x in r if x['event'] == 'end'}
raise SystemExit(0 if s and s == e else 1)"

echo
etb log
echo
echo "Workflow test passed."
