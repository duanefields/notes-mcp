#!/usr/bin/env bash
#
# Scan tracked files for values that look like real personal data.
#
# CLAUDE.md documents this as a grep to run by hand before committing. This is
# the same patterns, with the placeholders CLAUDE.md declares safe masked out
# first, so anything that survives is worth a look rather than another
# SYNTHETIC- fixture. That is what makes it usable as a gate instead of a check
# everyone learns to ignore.
#
# Only tracked files are scanned. docs/local/ is gitignored and is exactly
# where real hostnames and URLs are supposed to live, so it must stay out.

set -uo pipefail

# The patterns from CLAUDE.md: real-looking E.164 numbers, dashed phone
# numbers, consumer mail domains, home directory paths naming a person, and
# bare UUIDs, which is the shape every real note and folder identifier has.
PATTERN='\+1[0-9]{10}|[0-9]{3}-[0-9]{3}-[0-9]{4}|@(gmail|icloud|me)\.|/Users/[a-z]|[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}'

# Masking runs on the matched lines rather than on whole files, so a line
# carrying a placeholder *and* a real value still trips on the real one.
hits=$(
    git ls-files -z \
        | xargs -0 grep -nEiI "$PATTERN" 2>/dev/null \
        | sed -E \
            -e 's/\+1[0-9]{3}55501[0-9]{2}/(fiction-e164)/g' \
            -e 's/\(?[0-9]{3}\)?[-. ]?555[-. ]?01[0-9]{2}/(fiction-phone)/g' \
            -e 's#/Users/USERNAME#(placeholder-path)#g' \
            -e 's/SYNTHETIC-[A-Z0-9-]+/(synthetic-id)/g' \
            -e 's/AAAAAAAA-0000-4000-8000-0{11}[0-9]/(synthetic-uuid)/g' \
        | grep -EiI "$PATTERN"
)

# A hostname cannot be caught by shape. A machine is often named for an
# ordinary English word, which no pattern can tell from prose, and writing the
# real one into this script to grep for would leak the very thing the script
# exists to protect. So the list of literal strings that must
# never appear lives in docs/local/forbidden.txt, which is gitignored: one
# per line, blank lines and # comments skipped. Absent, this check is skipped,
# which is why it supplements the patterns above rather than replacing them.
FORBIDDEN="${FORBIDDEN_FILE:-docs/local/forbidden.txt}"
if [ -f "$FORBIDDEN" ]; then
    while IFS= read -r term; do
        [ -z "$term" ] && continue
        case "$term" in \#*) continue ;; esac
        found=$(git ls-files -z | xargs -0 grep -nIiF -- "$term" 2>/dev/null)
        if [ -n "$found" ]; then
            # The term itself is deliberately not echoed: this output can end
            # up in a public CI log.
            hits="${hits}
$(printf '%s' "$found" | sed -E "s/$term/(REDACTED-LOCAL-TERM)/Ig")"
        fi
    done < "$FORBIDDEN"
fi

if [ -n "$hits" ]; then
    echo "Possible personal data in tracked files:"
    echo
    echo "$hits"
    echo
    echo "Every line above needs a look. If it is a placeholder this script"
    echo "does not know about yet, add it to the masks in $0."
    exit 1
fi

echo "No personal data found in tracked files."
