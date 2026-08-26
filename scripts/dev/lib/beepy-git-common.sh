#!/usr/bin/env bash

# Shared safety checks for the Beepy contributor helpers.
# This file is sourced by the helper entry points; it is not a user command.

BEEPY_EXPECTED_BRANCH=development

beepy_stop() {
    printf '\nSTOP: %s\n' "$1" >&2
    return 1
}

beepy_require_repository() {
    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        beepy_stop "This command must be run inside a Git working repository."
        return 1
    fi

    BEEPY_REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || {
        beepy_stop "Git could not determine the repository root."
        return 1
    }
    readonly BEEPY_REPO_ROOT
}

beepy_repository_is_healthy() {
    git fsck --connectivity-only --no-dangling >/dev/null 2>&1
}

beepy_current_branch() {
    git symbolic-ref --quiet --short HEAD 2>/dev/null || true
}

beepy_git_path_exists() {
    local git_path
    git_path=$(git rev-parse --git-path "$1" 2>/dev/null) || return 1
    [[ -e "$git_path" ]]
}

beepy_operation_in_progress() {
    if beepy_git_path_exists MERGE_HEAD; then
        printf '%s\n' merge
    elif beepy_git_path_exists rebase-merge || beepy_git_path_exists rebase-apply; then
        printf '%s\n' rebase
    elif beepy_git_path_exists CHERRY_PICK_HEAD; then
        printf '%s\n' cherry-pick
    elif beepy_git_path_exists REVERT_HEAD; then
        printf '%s\n' revert
    elif beepy_git_path_exists BISECT_LOG || beepy_git_path_exists BISECT_START; then
        printf '%s\n' bisect
    else
        printf '%s\n' none
    fi
}

beepy_conflict_count() {
    git --no-pager diff --name-only --diff-filter=U -z -- |
        awk 'BEGIN { RS="\0" } length($0) { count++ } END { print count + 0 }'
}

beepy_explain_conflicts() {
    printf '%s\n' "Git found two changes that require a human decision." >&2
    printf '%s\n' "Your files have not been thrown away." >&2
    printf '%s\n' "Do not keep experimenting with Git commands." >&2
    printf '%s\n' "Ask for assistance before continuing." >&2
    printf '\n%s\n' "Conflicted paths:" >&2
    git -c core.quotePath=true --no-pager diff --name-only --diff-filter=U -- >&2
}

beepy_guard_mutating_state() {
    local branch operation conflicts

    branch=$(beepy_current_branch)
    if [[ -z "$branch" ]]; then
        beepy_stop "HEAD is detached. Switch to development before using this helper."
        return 1
    fi
    if [[ "$branch" == main ]]; then
        printf '%s\n' "You are on main." >&2
        printf '\n%s\n' "main is the stable branch and this helper will not modify it." >&2
        printf '%s\n' "Switch to development before making normal Beepy changes." >&2
        return 1
    fi
    if [[ "$branch" != "$BEEPY_EXPECTED_BRANCH" ]]; then
        beepy_stop "Current branch is '$branch'. This helper only modifies development."
        return 1
    fi

    if ! beepy_repository_is_healthy; then
        beepy_stop "Git repository integrity checking failed. Ask for assistance before continuing."
        return 1
    fi

    conflicts=$(beepy_conflict_count)
    if (( conflicts > 0 )); then
        beepy_explain_conflicts
        return 1
    fi

    operation=$(beepy_operation_in_progress)
    if [[ "$operation" != none ]]; then
        beepy_stop "A Git $operation operation is in progress. Ask for assistance before continuing."
        return 1
    fi
}

beepy_load_identity() {
    local name email compact_name compact_email lowered_name lowered_email

    name=$(git config --get user.name 2>/dev/null || true)
    email=$(git config --get user.email 2>/dev/null || true)
    compact_name=${name//[[:space:]]/}
    compact_email=${email//[[:space:]]/}

    if [[ -z "$compact_name" || -z "$compact_email" ]]; then
        beepy_stop "Git user.name and user.email must both be intentionally configured before saving."
        printf '%s\n' "See CONTRIBUTING.md for the safe one-time setup." >&2
        return 1
    fi
    if [[ "$name" == *$'\n'* || "$name" == *$'\r'* || "$email" == *$'\n'* || "$email" == *$'\r'* ]]; then
        beepy_stop "The configured Git identity contains an unsupported line break."
        return 1
    fi

    lowered_name=${name,,}
    lowered_email=${email,,}
    if [[ "$lowered_email" == beepy-history@invalid.example ||
          "$lowered_email" == beepy-migration@invalid.example ||
          "$lowered_name" == "beepy repository migration" ||
          ( "$lowered_name" == "jerry sandy" && "$lowered_email" == beepy-history@invalid.example ) ]]; then
        beepy_stop "The configured identity is reserved for reconstructed history or repository migration, not human development."
        printf '%s\n' "Configure your own chosen human identity before saving." >&2
        return 1
    fi

    BEEPY_IDENTITY_NAME=$name
    BEEPY_IDENTITY_EMAIL=$email
}

beepy_remote_url_is_safe() {
    local url=$1

    [[ "$url" != *$'\n'* && "$url" != *$'\r'* && "$url" != *$'\t'* ]] || return 1

    # Absolute local paths and file URLs keep the helpers testable without a network.
    if [[ "$url" == /* || "$url" == file:///* ]]; then
        return 0
    fi

    # Public Beepy GitHub forms. HTTP user information is intentionally rejected.
    shopt -s nocasematch
    if [[ "$url" =~ ^https://github\.com/[A-Za-z0-9_.-]+/beepy(\.git)?$ ||
          "$url" =~ ^git@github\.com:[A-Za-z0-9_.-]+/beepy(\.git)?$ ||
          "$url" =~ ^ssh://git@github\.com/[A-Za-z0-9_.-]+/beepy(\.git)?$ ]]; then
        shopt -u nocasematch
        return 0
    fi
    shopt -u nocasematch
    return 1
}

beepy_load_origin() {
    local -a urls push_urls
    local url

    mapfile -t urls < <(git config --get-all remote.origin.url 2>/dev/null || true)
    mapfile -t push_urls < <(git config --get-all remote.origin.pushurl 2>/dev/null || true)

    if (( ${#urls[@]} != 1 )); then
        beepy_stop "origin must have exactly one configured URL. No remote setting was changed."
        return 1
    fi
    if (( ${#push_urls[@]} != 0 )); then
        beepy_stop "origin has a separate push URL. This beginner helper will not use ambiguous remote settings."
        return 1
    fi

    url=${urls[0]}
    if ! beepy_remote_url_is_safe "$url"; then
        beepy_stop "origin is missing, credential-bearing, or not recognized as a safe Beepy/local-test remote."
        printf '%s\n' "The remote URL was not displayed in case it contains credentials." >&2
        return 1
    fi

    BEEPY_ORIGIN_URL=$url
    readonly BEEPY_ORIGIN_URL
}

beepy_validate_development_upstream() {
    local upstream
    upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)
    if [[ -n "$upstream" && "$upstream" != origin/development ]]; then
        beepy_stop "development tracks '$upstream', not origin/development. No remote operation was attempted."
        return 1
    fi
}

beepy_confirm() {
    local prompt=$1 reply
    printf '%s' "$prompt"
    if ! IFS= read -r reply; then
        printf '\n'
        return 1
    fi
    case ${reply,,} in
        y|yes) return 0 ;;
        *) return 1 ;;
    esac
}

beepy_print_effects() {
    local working_tree=$1 index=$2 local_history=$3 remote_branch=$4
    printf '%s\n' "What this does:"
    printf '%s\n' "- Working tree: $working_tree"
    printf '%s\n' "- Git index/staging area: $index"
    printf '%s\n' "- Local history: $local_history"
    printf '%s\n' "- Remote development branch: $remote_branch"
}
