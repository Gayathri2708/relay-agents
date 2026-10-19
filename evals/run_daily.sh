#!/usr/bin/env bash
# Free-tier eval driver: routed on all incidents, then single. Waits out the daily quota and resumes.
#
#   nohup evals/run_daily.sh >> evals/results/free-tier/run.log 2>&1 &
#
# Whenever new results are recorded it regenerates the README Results block and commits and pushes
# only README.md and the result files. Stop it with: pkill -f evals/run_daily.sh
set -u
cd "$(dirname "$0")/.."
OUT=evals/results/free-tier
PY=.venv/bin/python
mkdir -p "$OUT"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S')Z] $*"; }

publish() {
  [ -f "$OUT/results.jsonl" ] || return 0
  if [ -z "$(git status --porcelain -- "$OUT/results.jsonl")" ]; then return 0; fi  # nothing new
  $PY -m evals.report "$OUT" > /dev/null && $PY -m evals.update_readme "$OUT" > /dev/null || { log "report failed"; return 0; }
  local progress
  progress=$($PY -c "
import json; s=json.load(open('$OUT/summary.json'))['summary']
print(', '.join(f'{c} {s[c][\"incidents\"]}/49' for c in ('routed','single') if c in s))")
  git add README.md "$OUT/results.jsonl" "$OUT/meta.json" "$OUT/summary.json" "$OUT/summary.md"
  git commit -q -m "Eval results (free tier, partial): $progress

Measured by evals/run_daily.sh; README Results block regenerated from $OUT/summary.json.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" -- README.md "$OUT/results.jsonl" "$OUT/meta.json" "$OUT/summary.json" "$OUT/summary.md" \
    && log "committed: $progress"
  git push -q 2>&1 | tail -1 || log "push failed; will retry on next publish"
}

crashes=0
for cfg in routed single; do
  while true; do
    if ! curl -sf -m 10 http://localhost:8000/health > /dev/null; then
      log "DocPilot is not reachable on :8000; waiting 15 min"; sleep 900; continue
    fi
    log "running $cfg"
    $PY -m evals.run --out "$OUT" --configs "$cfg" --tpm 7500 --rpm 25
    code=$?
    publish
    case $code in
      0) log "$cfg complete"; crashes=0; break ;;
      2) log "daily quota reached; retrying in 1 h"; crashes=0; sleep 3600 ;;
      3) log "some incidents skipped after provider errors; retrying in 15 min"; sleep 900 ;;
      *) crashes=$((crashes + 1)); log "runner exited $code (crash $crashes)"
         [ $crashes -ge 20 ] && { log "giving up after 20 crashes"; exit 1; }
         sleep 900 ;;
    esac
  done
done
publish
log "eval finished"
