#!/usr/bin/env bash
# CI check (dev-team.yml, install-linux): every dev-* profile links the
# TEAM-only Mnemosyne bank ($ROOT/dev-team-mnemosyne), never the operator's
# own ($ROOT/mnemosyne), and the team bank actually works end to end.
#
# Usage:
#   check-team-bank.sh --root ROOT --cli MNEMOSYNE_CLI --init PROFILE \
#                      [--private PROFILE]... NAME=PROFILE_DIR...
#
#   --root     the Hermes root (dirname of `hermes config path`)
#   --cli      the mnemosyne CLI from the operator's Mnemosyne venv
#   --init     a linked profile to initialise the team DB through
#   --private  a profile EXPECTED to keep its own real bank (not shared)
#
# Behaviourally tested by
# dev-team/plugin/dev-team-gate/tests/test_ci_team_bank_checks.py, which
# is why this lives in a script and not inline in the workflow (PR #8 fix
# round 4, review t_899815da C1-C4).
set -euo pipefail

fail() { echo "::error::$*" >&2; exit 1; }

root='' cli='' init=''
private=()
pairs=()
while (($#)); do
    case "$1" in
        --root) root="$2"; shift 2 ;;
        --cli) cli="$2"; shift 2 ;;
        --init) init="$2"; shift 2 ;;
        --private) private+=("$2"); shift 2 ;;
        *=*) pairs+=("$1"); shift ;;
        *) fail "unknown argument: $1" ;;
    esac
done
[[ -n $root && -n $cli && -n $init ]] || fail "usage: --root, --cli and --init are required"
((${#pairs[@]})) || fail "usage: no NAME=PROFILE_DIR given"

real_dir() { (cd -- "$1" 2>/dev/null && pwd -P); }
# True if $1 equals $2 or lies under it (both already real paths).
is_or_under() { [[ $1 == "$2" || $1 == "$2"/* ]]; }
overlaps() { is_or_under "$1" "$2" || is_or_under "$2" "$1"; }
is_private() { local p; for p in "${private[@]+"${private[@]}"}"; do [[ $p == "$1" ]] && return 0; done; return 1; }
env_pin() {  # profile_dir var -> value of VAR= in its .env (last wins), unquoted
    local line val=''
    [[ -f $1/.env ]] || return 0
    while IFS= read -r line || [[ -n $line ]]; do
        [[ $line == "$2="* ]] && val="${line#"$2="}"
    done < "$1/.env"
    unquote_env_pin "$val"
}
# Item 4a (review r3) / operator decision (round 2): install-dev-team-
# unix.sh now writes every pin UNQUOTED again (the single-quoted,
# python-dotenv-escaped format from review r3 was reverted: Mnemosyne's
# own .env reader never unescapes, so it would read an escaped pin back
# corrupted). This unquoting function is therefore LEGACY-ONLY: kept so a
# pin written by the brief window where 046cadc's quoted format shipped
# (dotenv/parser.py:25,32 escapes: '\' -> '\\', "'" -> "\'") still
# resolves correctly here, not because any pin written by the current
# installer can be quoted -- it cannot: env_value_unsafe_reason() in
# install-dev-team-unix.sh refuses any value that starts or ends with a
# quote character before it ever reaches set_env_var(). A plain unquoted
# value (every current pin, and any pre-existing legacy one) passes
# through unchanged.
unquote_env_pin() {
    local v=$1
    if (( ${#v} >= 2 )) && [[ ${v:0:1} == "'" && ${v: -1} == "'" ]]; then
        v=${v:1:-1}
        local out='' i=0 ch nxt
        while (( i < ${#v} )); do
            ch=${v:i:1}
            if [[ $ch == '\' ]] && (( i + 1 < ${#v} )); then
                nxt=${v:i+1:1}
                if [[ $nxt == "'" || $nxt == '\' ]]; then
                    out+="$nxt"
                    i=$((i + 2))
                    continue
                fi
            fi
            out+="$ch"
            i=$((i + 1))
        done
        v=$out
    fi
    printf '%s' "$v"
}

# 1. The team bank is a directory of its own.
[[ -d $root/dev-team-mnemosyne ]] || fail "team bank directory missing: $root/dev-team-mnemosyne"
team="$(real_dir "$root/dev-team-mnemosyne")"
operator=''
if [[ -d $root/mnemosyne ]]; then operator="$(real_dir "$root/mnemosyne")"; fi

# 2. C2: the two banks never resolve to (or inside) each other.
if [[ -n $operator ]] && overlaps "$team" "$operator"; then
    fail "the team bank ($root/dev-team-mnemosyne -> $team) resolves to the operator's own bank ($operator)"
fi

# 3. C3/C4: every profile's mnemosyne/ is a link to the team bank, or is an
#    explicitly expected private real directory.
init_dir=''
for pair in "${pairs[@]}"; do
    name="${pair%%=*}" dir="${pair#*=}"
    [[ $name == "$init" ]] && init_dir="$dir"
    link="$dir/mnemosyne"
    [[ -e $link ]] || fail "$name: $link does not exist"
    got="$(real_dir "$link")" || fail "$name: cannot resolve $link"
    if is_private "$name"; then
        [[ -L $link ]] && fail "$name: expected a private bank, but $link is a link (to $got)"
        overlaps "$got" "$team" && fail "$name: expected a private bank, but $link resolves into the team bank"
        if [[ -n $operator ]] && overlaps "$got" "$operator"; then
            fail "$name: $link resolves to the operator's own bank ($operator)"
        fi
        echo "$name: private bank (expected): $got"
        continue
    fi
    if [[ -n $operator ]] && overlaps "$got" "$operator"; then
        fail "$name: $link resolves to the operator's own bank ($operator)"
    fi
    if [[ ! -L $link || $got != "$team" ]]; then
        fail "$name: $link is not a symlink to the team bank $team (resolves to $got)"
    fi
    echo "$name: linked to the team bank"
done
[[ -n $init_dir ]] || fail "--init $init is not one of the given profiles"
is_private "$init" && fail "--init $init is a private profile; initialise through a linked one"

# 4. C1: create the team DB the way a real profile would -- the mnemosyne
#    CLI with the profile as HERMES_HOME and its own .env pins -- then
#    require it under dev-team-mnemosyne/data, and the operator DB
#    untouched. Pins are checked BEFORE the CLI runs, so a pin into the
#    operator's bank fails without writing to it.
pin_data="$(env_pin "$init_dir" MNEMOSYNE_DATA_DIR)"
pin_home="$(env_pin "$init_dir" MNEMOSYNE_HOME)"
[[ -n $pin_data && -n $pin_home ]] || fail "$init: .env does not pin MNEMOSYNE_DATA_DIR and MNEMOSYNE_HOME"
got_home="$(real_dir "$pin_home")" || fail "$init: pinned MNEMOSYNE_HOME does not exist: $pin_home"
[[ $got_home == "$team" ]] || fail "$init: .env pins MNEMOSYNE_HOME=$pin_home, not the team bank $team"
got_data_parent="$(real_dir "$(dirname -- "$pin_data")")" || fail "$init: cannot resolve pinned MNEMOSYNE_DATA_DIR=$pin_data"
[[ $got_data_parent == "$team" && $(basename -- "$pin_data") == data ]] \
    || fail "$init: .env pins MNEMOSYNE_DATA_DIR=$pin_data, not $team/data"

operator_db="$root/mnemosyne/data/mnemosyne.db"
before=''
[[ -f $operator_db ]] && before="$(sha256sum < "$operator_db")"

echo "Initialising the team DB through $init ($cli stats)..."
env -u MNEMOSYNE_SHARED_DB_PATH \
    HERMES_HOME="$init_dir" MNEMOSYNE_DATA_DIR="$pin_data" MNEMOSYNE_HOME="$pin_home" \
    "$cli" stats || fail "$init: '$cli stats' failed"

[[ -f $root/dev-team-mnemosyne/data/mnemosyne.db ]] \
    || fail "team Mnemosyne DB was not created at $root/dev-team-mnemosyne/data/mnemosyne.db"
after=''
[[ -f $operator_db ]] && after="$(sha256sum < "$operator_db")"
[[ $before == "$after" ]] || fail "the operator's DB ($operator_db) changed while initialising the team bank"

# 5. The DB is reachable through every linked profile's own link.
for pair in "${pairs[@]}"; do
    name="${pair%%=*}" dir="${pair#*=}"
    is_private "$name" && continue
    [[ -f $dir/mnemosyne/data/mnemosyne.db ]] || fail "$name: team Mnemosyne DB not reachable through $dir/mnemosyne"
done
echo "Team bank OK: $team"
