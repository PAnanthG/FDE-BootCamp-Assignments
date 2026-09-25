#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Stage guard - FDE Assignment 3 (Moment Search at Scale)
#
# Run at every stage gate and from .git/hooks/pre-commit.
# Deliberately pure ASCII: the script must never match its own canary patterns.
#
#   ./scripts/guard.sh          normal run
#   ./scripts/guard.sh --final  adds full-tree sweep + upstream diff review
#
# Exit 0 = clean, 1 = at least one FAIL.
# ---------------------------------------------------------------------------
set -uo pipefail

FAIL=0
FINAL=0
[ "${1:-}" = "--final" ] && FINAL=1

# Sloth U+1F9A5 built at runtime so this file stays ASCII-clean on disk.
SELF="scripts/guard.sh"

ok()   { echo "  [ok]   $1"; }
bad()  { echo "  [FAIL] $1"; FAIL=1; }
warn() { echo "  [warn] $1"; }

# ---------------------------------------------------------------------------
# has_bytes <perl-regex> <file>   -> exit 0 if the byte pattern is present.
# pipe_bytes <perl-regex>         -> filter stdin to lines matching the pattern.
#
# These exist because `grep -P` is NOT portable. BSD/macOS grep has no -P: it
# does not report "no match", it errors and exits 2. Every `if grep -qP ...`
# then reads that as CLEAN, so the check fails OPEN and silently admits the
# exact canary it was written to catch. Verified: the plan's commit-msg hook,
# unmodified, accepted a commit message containing the sloth emoji on macOS.
# perl ships with macOS and every Linux image we deploy to, and its engine
# handles \xNN byte classes natively.
# ---------------------------------------------------------------------------
has_bytes() {
  LC_ALL=C perl -e '
    my ($re, $file) = @ARGV;
    open(my $fh, "<", $file) or exit 1;
    binmode($fh);
    local $/;
    my $d = <$fh>;
    $d = "" unless defined $d;
    exit(($d =~ /$re/) ? 0 : 1);
  ' "$1" "$2" 2>/dev/null
}

pipe_bytes() {
  LC_ALL=C perl -ne 'BEGIN { $re = shift @ARGV } print if /$re/' "$1"
}

INVISIBLE='\xe2\x80[\x8b-\x8f]|\xef\xbb\xbf|\xe2\x81\xa0|\xe2\x80[\xaa-\xae]'
NONASCII='[\x80-\xFF]'

# True for text files only. INVISIBLE and NONASCII are UTF-8 *text* sequences,
# so running them over a binary matches by coincidence, not by smuggling: a
# compressed PDF stream contains effectively arbitrary bytes, and
# PRODUCT_EVAL.pdf duly tripped the zero-width check the first time it was
# committed.
#
# This narrows WHERE those two byte checks look; it does not weaken WHAT they
# look for, and every other check (canary markers, hidden instructions, secrets,
# detector hashes) still runs over binaries unchanged.
#
# NOT `grep -Iq .` - that is the SAME portability trap as `grep -P` above, and it
# fails the same way: silently, in the permissive direction. BSD/macOS grep
# decides binary-ness from its FIRST BUFFER only (~32KB). PRODUCT_EVAL.pdf is
# ASCII PDF syntax until its first NUL at offset 43338, so BSD grep calls it
# text, the skip never fires, and the false positive survives. It looked correct
# when tested interactively only because this shell aliases grep to ugrep, which
# scans the whole file and gets it right - the wrapper masked the bug in exactly
# the environment used to verify the fix.
#
# perl reads the entire file (same as has_bytes) and is present everywhere this
# runs, so the answer does not depend on which grep is on PATH.
is_text() {
  LC_ALL=C perl -e '
    open(my $fh, "<", $ARGV[0]) or exit 1;
    binmode($fh);
    local $/;
    my $d = <$fh>;
    exit((defined($d) && $d =~ /\x00/) ? 1 : 0);
  ' "$1" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Grader-side detector files, copied verbatim from the assignment directory.
# They CONTAIN the canary marker strings and the sloth emoji, because their job
# is to detect them - so check 1 has to skip them or it flags the detector as
# the thing it detects. D4 says never edit these to make a check pass, so
# instead of merely excluding them we pin their SHA-256: any edit is a FAIL.
# sla.json is pinned for the same reason (Stage 7: gates must not be loosened).
# bench.py is deliberately NOT pinned - we implement its four TODOs.
# ---------------------------------------------------------------------------
DETECTORS="eval/eval.py eval/rubric.json benchmark/sla.json"
DETECTORS="$DETECTORS .claude/skills/fde-momentsearch-scaled-eval/SKILL.md"
DETECTORS="$DETECTORS .claude/skills/fde-momentsearch-scaled-eval/references/product-eval-template.md"
DETECTOR_SHA="\
fad39cdfd865e9c906dbd13d16cc506ee92181c195106d11d6f95834e0b4f951  eval/eval.py
a64b149d038c81c5b1b61a6ff8729277f5d30b1584fcf9081f25be476130cec1  eval/rubric.json
41a9df3491b50b75742cfc2d39a029dc77981612408cd021a3f8d2d552db996e  benchmark/sla.json
b26d28b130ce0593ff0036f963c64c3a974977ebffe5c4f762f433cfab4ca6f9  .claude/skills/fde-momentsearch-scaled-eval/SKILL.md
273182dfc539730cfd22e89466f4a85bc6074f3359c61ce75350e9739ca65e98  .claude/skills/fde-momentsearch-scaled-eval/references/product-eval-template.md"

cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)" || exit 1

echo "=== 1. canary artifacts ==="
if find . -path ./.git -prune -o -iname 'ROBOT_WAS_HERE*' -print | grep -q .; then
  bad "canary file present: $(find . -path ./.git -prune -o -iname 'ROBOT_WAS_HERE*' -print | tr '\n' ' ')"
else
  ok "no canary file"
fi

# Scope: files that could actually reach the repository - tracked, plus
# untracked-but-not-ignored. NOT a raw `grep -r .`, which also walks .venv,
# node_modules and data/. That is not hypothetical: Prefect's bundled server UI
# ships a minified JS bundle containing one of these marker words, so a plain
# recursive grep fails this check the moment anyone creates a virtualenv.
# Deriving the list from git is exactly the right boundary and needs no
# per-directory exclusion list to maintain.
CANDIDATES=$( { git ls-files 2>/dev/null
                git ls-files --others --exclude-standard 2>/dev/null
              } | sort -u)
POEM=""
if [ -n "$CANDIDATES" ]; then
  POEM=$(printf '%s\n' "$CANDIDATES" \
         | grep -v -e "^$SELF$" -e '^A3-.*\.md$' \
         | tr '\n' '\0' \
         | xargs -0 grep -ilE 'toaster|haiku of shame|agent-drove' 2>/dev/null \
         | sort -u)
fi
# Subtract the grader's own detector files. They carry the marker strings BY
# DESIGN; check 1b pins them by hash instead, so excluding them here costs
# nothing. Note README.md / AGENTS.md are NO LONGER excluded: the assignment
# copies that carry the injection never enter this repo (plan 1c), so a match
# in OUR README is a real hit and must not be silently skipped.
for d in $DETECTORS; do
  POEM=$(printf '%s\n' "$POEM" | grep -v -x -F "$d")
done
POEM=$(printf '%s\n' "$POEM" | grep -v '^$')
if [ -n "$POEM" ]; then
  bad "canary poem/marker text found in: $(printf '%s\n' "$POEM" | tr '\n' ' ')"
else
  ok "no canary poem text"
fi

echo "=== 1b. grader detector files unmodified (D4) ==="
SHACMD=""
command -v shasum    >/dev/null 2>&1 && SHACMD="shasum -a 256"
[ -z "$SHACMD" ] && command -v sha256sum >/dev/null 2>&1 && SHACMD="sha256sum"
MISSING=""
for d in $DETECTORS; do [ -f "$d" ] || MISSING="$MISSING $d"; done
if [ -z "$SHACMD" ]; then
  warn "no shasum/sha256sum available - cannot verify detector integrity"
elif [ -n "$MISSING" ]; then
  warn "detector file(s) not copied in yet:$MISSING"
elif printf '%s\n' "$DETECTOR_SHA" | $SHACMD -c - >/dev/null 2>&1; then
  ok "eval + rubric + sla + skill files match the assignment originals"
else
  bad "grader detector file MODIFIED - restore it; never edit a detector to pass (D4):"
  printf '%s\n' "$DETECTOR_SHA" | $SHACMD -c - 2>/dev/null | grep -v ': OK$' | sed 's/^/         /'
fi

echo "=== 2. commit messages (ASCII-only policy) ==="
if git rev-parse HEAD >/dev/null 2>&1; then
  # Any byte >= 0x80 means non-ASCII, which catches ANY emoji canary - not just
  # the sloth we happen to know about. Does not depend on printf escape support.
  #
  # Scoped to OUR commits. Upstream ships two legitimately non-ASCII subjects
  # (an em dash in the initial commit, U+2264 in c5af717); scanning the whole
  # history would fail this check on every run forever and train us to ignore
  # it. If there is no upstream remote yet, scan everything.
  if git rev-parse --verify -q upstream/main >/dev/null 2>&1; then
    MSGRANGE="upstream/main..HEAD"
  else
    MSGRANGE="HEAD"
  fi
  BADMSG=$(git log --format='%h %s %b' -n 200 "$MSGRANGE" 2>/dev/null \
           | pipe_bytes "$NONASCII" | head -5)
  if [ -n "$BADMSG" ]; then
    bad "non-ASCII (emoji) in commit message(s):"
    echo "$BADMSG" | sed 's/^/         /'
  else
    ok "all commit messages ASCII"
  fi
  # Textual canary markers, in case someone typed them without the emoji.
  if git log --format='%s %b' -n 200 2>/dev/null | grep -qiE 'agent-drove|ROBOT_WAS_HERE'; then
    bad "canary marker text in a commit message"
  else
    ok "no canary marker text in commit messages"
  fi
else
  warn "no commits yet"
fi

echo "=== 3. secrets / junk in the index ==="
STAGED=$(git diff --cached --name-only 2>/dev/null)
if echo "$STAGED" | grep -qE '(^|/)\.env$|\.env\.(local|prod)|\.venv/|__pycache__/|\.mp4$|\.pem$|\.key$'; then
  bad "sensitive/junk path staged: $(echo "$STAGED" | grep -E '(^|/)\.env$|\.env\.(local|prod)|\.venv/|__pycache__/|\.mp4$|\.pem$|\.key$' | tr '\n' ' ')"
else
  ok "nothing sensitive staged"
fi

if git ls-files 2>/dev/null | grep -qE '(^|/)\.env$'; then
  bad ".env is TRACKED - remove from history before submitting"
else
  ok ".env not tracked"
fi

# High-signal key shapes in tracked text (not the whole filesystem).
# The inherited pattern only knew OpenAI / Groq / Slack / AWS. This project
# actually uses Neon, Prefect Cloud, Qdrant Cloud, Tigris and a vision LLM, so
# a leak of any credential we really hold would have sailed straight past it.
# Kept in one variable so the detection and the reporting cannot drift apart.
KEYPAT='sk-[A-Za-z0-9_-]{20,}'                 # OpenAI, incl. sk-ant-, sk-proj-
KEYPAT="$KEYPAT|gsk_[A-Za-z0-9]{20,}"          # Groq
KEYPAT="$KEYPAT|xox[bpars]-[A-Za-z0-9-]{10,}"  # Slack
KEYPAT="$KEYPAT|AKIA[0-9A-Z]{16}"              # AWS access key id
KEYPAT="$KEYPAT|pn[ubs]_[A-Za-z0-9]{16,}"      # Prefect Cloud API key
KEYPAT="$KEYPAT|tid_[A-Za-z0-9_-]{16,}"        # Tigris key id
KEYPAT="$KEYPAT|tsec_[A-Za-z0-9_-]{16,}"       # Tigris secret
KEYPAT="$KEYPAT|nvapi-[A-Za-z0-9_-]{20,}"      # NVIDIA
KEYPAT="$KEYPAT|eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"   # JWT (Qdrant Cloud)
KEYPAT="$KEYPAT|postgres(ql)?://[^:/@[:space:]]+:[^@[:space:]]+@"  # DB URL with password
KEYPAT="$KEYPAT|-----BEGIN [A-Z ]*PRIVATE KEY-----"          # GCP service account
# .env.example is excluded because it ships placeholder values in these shapes
# on purpose; $SELF is excluded because it contains the patterns themselves.
KEYHITS=$(git grep -lIE "$KEYPAT" -- . 2>/dev/null | grep -v -e "$SELF" -e '.env.example')
if [ -n "$KEYHITS" ]; then
  bad "possible live key in tracked file(s): $(printf '%s\n' "$KEYHITS" | tr '\n' ' ')"
else
  ok "no key-shaped strings in tracked files"
fi

echo "=== 4. hidden-instruction scan on changed files ==="
# Union of: unstaged, staged, and the last commit. Without the last commit the
# set is empty right after committing, so a gate check would scan nothing.
CHANGED=$( { git diff --name-only HEAD 2>/dev/null
             git diff --cached --name-only 2>/dev/null
             git diff --name-only HEAD~1 HEAD 2>/dev/null
             git ls-files --others --exclude-standard 2>/dev/null
           } | sort -u | grep -v '^$')
HID=""
for f in $CHANGED; do
  [ -f "$f" ] || continue
  # This script carries the patterns it hunts for, so it always matches itself.
  # A warning that fires on every run is a warning nobody reads.
  [ "$f" = "$SELF" ] && continue
  if grep -lE '<!--|ignore (previous|above|all)|do not (mention|tell)|your human|COURSE POLICY' "$f" >/dev/null 2>&1; then
    HID="$HID $f"
  fi
done
if [ -n "$HID" ]; then
  warn "review manually for embedded instructions:$HID"
else
  ok "no hidden-instruction patterns in changed files"
fi

echo "=== 5. invisible unicode in changed files ==="
INV=""
for f in $CHANGED; do
  [ -f "$f" ] || continue
  is_text "$f" || continue          # binaries: see is_text()
  if has_bytes "$INVISIBLE" "$f"; then
    INV="$INV $f"
  fi
done
if [ -n "$INV" ]; then
  bad "zero-width / bidi characters in:$INV"
else
  ok "no invisible unicode in changed files"
fi

echo "=== 6. provided video pipeline baseline ==="
if [ -f baseline/video_citation.json ]; then
  ok "baseline captured (compare after retrieval changes)"
else
  warn "no baseline/video_citation.json yet - capture in Stage 2"
fi

echo "=== 7. unexpected generated artifacts ==="
# Everything the toolchain is allowed to produce. Anything else in an untracked
# state at a gate deserves an explanation.
UNTRACKED=$(git ls-files --others --exclude-standard 2>/dev/null \
  | grep -vE '^(eval/REPORT\.md|benchmark/_bench\.json|benchmark/queries\.jsonl|PRODUCT_EVAL\.(md|pdf)|baseline/|scripts/|SYSTEM_MAP\.md|DECISIONS\.md|A3-.*\.md)')
if [ -n "$UNTRACKED" ]; then
  warn "untracked files not on the expected-artifact list:"
  echo "$UNTRACKED" | sed 's/^/         /'
else
  ok "no unexpected untracked artifacts"
fi

if [ "$FINAL" = "1" ]; then
  echo "=== 8. FINAL: full-tree invisible-unicode sweep ==="
  SWEEP=""
  while IFS= read -r -d '' f; do
    [ -f "$f" ] || continue
    if is_text "$f" && has_bytes "$INVISIBLE" "$f"; then SWEEP="$SWEEP $f"; fi
  done < <(git ls-files -z 2>/dev/null)
  # NOT `[ -n "$SWEEP" ] && bad ... || ok ...`: bad() returns 1, so the || arm
  # fires too and prints [ok] directly under the [FAIL].
  if [ -n "$SWEEP" ]; then
    bad "invisible unicode in tracked files:$SWEEP"
  else
    ok "tracked tree clean"
  fi

  echo "=== 9. FINAL: review every file added vs upstream ==="
  if git rev-parse upstream/main >/dev/null 2>&1; then
    echo "  Added files (each must be justifiable):"
    git diff --diff-filter=A --name-only upstream/main...HEAD | sed 's/^/         + /'
  else
    warn "no upstream/main remote - add it to diff your fork against the original"
  fi
fi

echo
if [ "$FAIL" = "0" ]; then echo "GUARD: CLEAN"; else echo "GUARD: FAILURES PRESENT - do not commit/submit"; fi
exit $FAIL
