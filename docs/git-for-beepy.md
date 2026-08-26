# Git for Beepy

This guide explains the ordinary Git concepts behind Beepy's safety helpers.
The goal is to make each checkpoint understandable, reviewable, and portable.

## Git is history, not just backup storage

A folder backup answers, "What files were copied at that moment?" Git also
records a connected history: who intentionally saved each checkpoint, why it
was saved, and which earlier checkpoint it follows. Branches let active and
stable histories move at different rates without making duplicate working
folders the source of truth.

For Beepy, the basic flow is:

```text
working tree        staging area          local development       GitHub
files being edited  selected changes      saved commits            origin/development
       |                    |                     |                         |
       +---- git add ------>|---- git commit --->|------ git push -------->|
```

The helpers pause before each mutating step and print the real Git command.

## The working tree

The working tree contains the files you can open and edit. A changed working
file is not yet a Git checkpoint.

`beepy-status` reports three useful kinds of working-tree state:

- unstaged changes: tracked files that differ from the last selected version;
- untracked files: new files Git has not been asked to track;
- conflicts: files containing competing edits that need a human decision.

Inspect the current state with:

```bash
./scripts/dev/beepy-status
```

This command is read-only. It does not fetch, create files, update refs, or
touch the index.

## The staging area

The staging area, also called the Git index, is the selection for the next
commit. It sits between editing and committing:

```text
working tree -- select reviewed changes --> staging area -- save --> commit
```

Staging does not upload anything and does not modify `main`.

`beepy-save` uses one understandable all-reviewed-changes workflow. It first
shows Git's short status, a compact tracked-file diff statistic, new untracked
files, and every candidate path. It checks the candidate paths against the
sensitive-file policy before offering to stage them.

Only after confirmation does it run:

```bash
git add --all -- .
```

That updates the staging area. It does not create a commit or contact GitHub.

## A commit is a checkpoint

A commit saves the staged snapshot into local Git history. It includes the
configured contributor identity and a message explaining the purpose of the
checkpoint.

`beepy-save` requires a nonempty one-line message of reasonable length, shows
the message and actual command, and asks again before committing. It uses the
normal Git commit operation with identity fallback disabled:

```text
git -c user.useConfigOnly=true commit -m <your reviewed message>
```

The message is passed as data, not executed as a shell command. After the
commit, the checkpoint exists locally. GitHub has not changed.

Good messages briefly describe the result, for example:

```text
Fix ticket search filtering
Document local setup checks
Handle an empty Kal response
```

## The Beepy branches

```text
development: active work and normal contributor checkpoints
      |
      | reviewed pull request
      v
main:        stable, reviewed code
```

The helpers mutate only `development`. On `main`, detached HEAD, or any
unexpected branch, `beepy-save` and `beepy-sync` stop. They never switch
branches automatically.

Stable promotion is outside these beginner helpers. `main` is normally updated
through the reviewed repository workflow using a pull request from
`development`.

## Local development and origin/development

`development` is the branch checked out in the local repository.
`origin/development` is the locally remembered view of GitHub's development
branch. That remembered view may be stale until Git fetches.

```text
local repository                           GitHub repository
development          <--- compare --->     development
origin/development   last fetched view
```

`beepy-status` never fetches, so it says that ahead/behind comparison uses the
last fetched Git state.

## Fetch and beepy-sync

Fetch downloads Git objects and reference information. It does not merge and
does not change working files.

Before fetching, `beepy-sync` validates the repository, branch, operation
state, conflicts, `origin`, and upstream. It explains and then runs only:

```bash
git fetch origin development
```

After fetching, it compares local `development` with the fetched development
commit.

### Equal

```text
LOCAL == REMOTE
```

Nothing needs updating.

### Local is behind only

```text
local:   A---B
remote:  A---B---C---D
```

GitHub has newer commits and the local branch has no competing commits. A
fast-forward can move local `development` directly from `B` to `D`. No merge
commit is needed.

If the working tree is clean, the helper explains and offers:

```bash
git merge --ff-only FETCH_HEAD
```

The `--ff-only` rule is important. If a fast-forward is not possible, Git stops
instead of creating an ordinary merge. The helper does not fall back to merge,
rebase, reset, stash, or force.

If unsaved working-tree changes exist, the helper stops before updating. First
create a checkpoint with `beepy-save`, or handle the changes deliberately with
assistance. It never stashes automatically and does not suggest a destructive
discard command.

### Local is ahead only

```text
local:   A---B---C
remote:  A---B
```

Local commits are waiting to be pushed. Sync reports this and changes nothing.

### Histories diverged

```text
             C---D  local
            /
       A---B
            \
             E---F  remote
```

Both copies contain commits the other lacks. Combining them requires a human
decision. `beepy-sync` stops and asks the user to get help. It does not guess,
merge, rebase, reset, stash, or force.

## Push

Push publishes local commits to a remote branch. After a successful local
checkpoint, `beepy-save` asks separately:

```text
Push this development commit to origin/development? [y/N]
```

Only an explicit yes runs the normal exact-branch push:

```bash
git push origin development:development
```

The helper rechecks the branch and validates the remote first. It never pushes
`main` and never force pushes. If remote development moved, the normal push is
rejected, nothing is overwritten, and the helper tells the user to run
`beepy-sync`.

## Pull requests

A pull request is a proposal to review one branch before bringing it into
another. Beepy's normal stable promotion proposes `development` as the source
and `main` as the destination.

The pull request review can check code, tests, privacy, and deployment impact.
The beginner helpers stop at maintaining `development`; they do not create,
merge, or approve pull requests and do not modify `main`.

## Conflicts and operations in progress

A conflict means Git found edits it cannot safely combine on its own. It is not
proof that work was lost.

When conflicts exist, the helpers show only conflicted paths and explain:

```text
Git found two changes that require a human decision.
Your files have not been thrown away.
Do not keep experimenting with Git commands.
Ask for assistance before continuing.
```

They do not edit conflict markers, select one side, stage a resolution, or
continue an operation. They also stop for merge, rebase, cherry-pick, revert,
or bisect state already in progress.

## Identity and historical credit

Git writes an author identity into each commit. Human Beepy commits require
explicit `user.name` and `user.email` configuration. `beepy-save` checks those
keys directly and commits with `user.useConfigOnly=true`, preventing Git from
falling back to a guessed `username@hostname` identity.

Use the local setup in `CONTRIBUTING.md`. The helper displays the configured
identity and asks for confirmation before staging.

Jerry Sandy's historical work is credited by the reconstructed-history
identity `Jerry Sandy <beepy-history@invalid.example>`. Repository setup uses
`Beepy Repository Migration <beepy-migration@invalid.example>`. These invalid
addresses are historical provenance markers and are rejected for future human
commits. Jerry's future email remains his explicit choice; it is not guessed in
the repository.

## Secrets never belong in Git

Git preserves history, so deleting a secret in a later commit does not safely
remove the earlier copy. Never stage credentials, tokens, private keys,
environment secrets, production databases, dumps, private exports, client
data, or backup archives.

`beepy-save` uses path-based checks and does not inspect secret contents. It
stops before staging suspicious candidates and provides no beginner bypass.
The exact public template `.env.example` is allowed, but it must contain only
safe example values.

## Safe practice sequence

```text
1. beepy-status             Understand branch, identity, and local state.
2. beepy-sync               Fetch and safely classify remote development.
3. edit and test            Work only on development.
4. beepy-save --dry-run     Preview candidate paths and workflow.
5. beepy-save               Confirm stage, commit, and optional push separately.
6. STOP means stop          Read the reason and ask for help.
```

Use `--help` for a short reminder:

```bash
./scripts/dev/beepy-status --help
./scripts/dev/beepy-save --help
./scripts/dev/beepy-sync --help
```
