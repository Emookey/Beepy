# Contributing to Beepy

Beepy uses ordinary Git with a small set of safety helpers. The helpers show
the real Git commands and explain what each command changes. They are training
wheels, not a replacement source-control system.

## Branches

- `development` is the normal active working branch. Make and save everyday
  changes here.
- `main` is stable, reviewed code. The beginner helpers never change it.
- `main` is normally updated through a reviewed pull request from
  `development`. These helpers intentionally do not perform that promotion.

Always check the branch before editing:

```bash
./scripts/dev/beepy-status
```

If it says you are on `main`, detached HEAD, or an unexpected branch, stop and
ask for help switching safely. The helpers do not switch branches for you.

## One-time Git identity setup

Every new human commit must use the contributor's intentionally chosen name
and email. Beepy does not allow Git to guess `username@hostname`, and the
helpers do not configure identity automatically.

For Jerry Sandy, set the approved public name locally in the Beepy repository:

```bash
git config --local user.name "Jerry Sandy"
```

Jerry must explicitly choose his future commit email before setting it. Do not
guess it. If privacy is desired, a GitHub-provided noreply address shown in his
own GitHub email settings is one option.

After Jerry chooses an address, he can set it with this template, replacing
the placeholder with the address he selected:

```bash
git config --local user.email "YOUR CHOSEN EMAIL"
```

Other contributors use their own chosen identity:

```bash
git config --local user.name "YOUR NAME"
git config --local user.email "YOUR CHOSEN EMAIL"
```

Recommended protection against Git fallback identities:

```bash
git config --local user.useConfigOnly true
```

Review the result without exposing anything else:

```bash
git config --local --get user.name
git config --local --get user.email
git config --local --get user.useConfigOnly
```

The historical identity `Jerry Sandy <beepy-history@invalid.example>` credits
Jerry's reconstructed work. `Beepy Repository Migration
<beepy-migration@invalid.example>` identifies repository setup commits. Those
addresses are provenance placeholders, not human development identities, and
`beepy-save` rejects them.

Future commits are credited from the human identity configured when the commit
is created. Existing historical commits are not rewritten.

## Normal beginner workflow

From the repository root:

```text
beepy-status -> beepy-sync -> edit and test -> beepy-save
```

1. Inspect your branch and local changes:

   ```bash
   ./scripts/dev/beepy-status
   ```

2. Check for remote `development` updates:

   ```bash
   ./scripts/dev/beepy-sync
   ```

3. Edit Beepy and run the relevant tests.

4. Preview the save workflow without changing Git:

   ```bash
   ./scripts/dev/beepy-save --dry-run
   ```

5. Create a reviewed checkpoint:

   ```bash
   ./scripts/dev/beepy-save
   ```

The save helper pauses separately before staging, committing, and pushing.
Read each explanation and answer only when ready.

## What the words mean

- The working tree is the set of files currently being edited.
- Staging selects reviewed changes for the next checkpoint. Staging does not
  upload anything.
- A commit is a saved local historical checkpoint with an author, time, and
  message. It does not update GitHub by itself.
- A push publishes local commits to `origin/development`. `beepy-save` asks
  separately before a normal push and never force pushes.
- A pull request is a reviewed proposal on GitHub. For Beepy, it normally asks
  to bring reviewed `development` history into stable `main`.

## When a helper says STOP

Stop means the helper found a state that needs a human decision. Read the
reason and do not keep trying unrelated Git commands.

If Git reports conflicts:

- Git found competing edits and cannot safely choose between them.
- Your files have not been thrown away.
- Do not choose "ours" or "theirs" blindly.
- Ask for assistance before continuing.

The helpers never resolve conflicts, stash changes, rebase, rewrite history,
discard work, or force push.

## Sensitive-file policy

Never commit secrets or private operational data. `beepy-save` checks candidate
paths before staging and refuses environment files, secret directories,
private keys, credentials, tokens, production databases, database dumps,
private email or client-data exports, and backup archives. It reports paths,
not contents, and has no automatic bypass.

The publication template `.env.example` is allowed. It must contain examples
only, never working credentials or private values.

If an expert truly needs to commit a normally blocked path, that decision must
use a deliberate manual Git workflow outside this beginner helper.

## Running the helpers

Direct repository usage is recommended:

```bash
./scripts/dev/beepy-status
./scripts/dev/beepy-save
./scripts/dev/beepy-sync
```

Each helper supports `--help`.

An optional future user-local setup can use symlinks after checking that the
destination names do not already exist:

```bash
mkdir -p "$HOME/.local/bin"
ln -s "/absolute/path/to/beepy/scripts/dev/beepy-status" "$HOME/.local/bin/beepy-status"
ln -s "/absolute/path/to/beepy/scripts/dev/beepy-save" "$HOME/.local/bin/beepy-save"
ln -s "/absolute/path/to/beepy/scripts/dev/beepy-sync" "$HOME/.local/bin/beepy-sync"
```

This is optional and was not performed during repository setup. It works as a
short command only if `$HOME/.local/bin` is already on the user's `PATH`.
Removal is explicit and reversible by unlinking those three symlinks. Never use
an overwrite option when creating them.

For a deeper explanation, read [docs/git-for-beepy.md](docs/git-for-beepy.md).
