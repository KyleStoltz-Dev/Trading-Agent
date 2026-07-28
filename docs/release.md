# Release engineering

This repository builds **candidate artifacts only**. CI does not tag a commit, publish a Python
package, deploy a service, or modify a live database. Those remain separately authorized human
actions.

## Release gates

A release candidate is acceptable only when all of the following are true:

1. The worktree is clean and the reviewed commit is the intended release commit.
2. `pyproject.toml` contains the intended version and `uv lock --check` succeeds.
3. Ruff, the security lint, the complete PostgreSQL-backed test suite, and the locked Python/npm
   dependency audits pass.
4. Linux, macOS, and Windows clean-installer jobs pass without starting the interactive setup.
5. The secure development Dockerfile builds from a credential-free context; its hosted-Linux
   runtime allow/deny/fail-closed checks and `tests/test_secure_devcontainer.py` pass.
6. A fresh database upgrades to the single Alembic head. The reviewed reversible migration tail
   downgrades to `a73f1c9d4e20`, re-upgrades, and `alembic check` reports no model drift.
7. A custom-format PostgreSQL backup restores into an isolated temporary database with matching
   public-table row counts and Alembic revisions.
8. Two builds from the same commit and `SOURCE_DATE_EPOCH` produce byte-identical wheel and sdist
   files.
9. Archive inspection confirms the wheel contains runtime policy, harness resources, and every
   migration, and confirms the sdist contains the locked source/release tooling.
10. `SHA256SUMS`, `release-manifest.json`, and `sbom.spdx.json` match the candidate files.
11. The built wheel imports and exposes `trade` from outside the source checkout.
12. Migration impact, configuration changes, recovery steps, and known limitations have been
    reviewed.

The repository currently has no `LICENSE` file. Do not publish to PyPI or represent the package
as having a particular open-source license until the owner deliberately selects and adds one.

## Locked clean installation

The desktop installers bootstrap the pinned `uv` version from a binary wheel matching an
official PyPI SHA-256 digest, then support a non-interactive release smoke mode:

```bash
./install-trading-agent.sh --no-setup
./install-trading-agent.command --no-setup
```

```powershell
.\install-trading-agent.ps1 -NoSetup
```

This mode creates `.venv` and synchronizes the checked-in lock without opening onboarding or
requiring a database/model credential. Normal users omit the flag and continue into guided setup.

## Build and inspect a candidate

Use an isolated clean clone. `SOURCE_DATE_EPOCH` must be the release commit time:

```bash
export SOURCE_DATE_EPOCH="$(git log -1 --pretty=%ct)"
rm -rf dist
uv lock --check
uv sync --locked --group release
uv build --no-build-isolation
python scripts/generate_sbom.py
python scripts/verify_release_artifacts.py --write-metadata
python scripts/verify_release_artifacts.py --check-metadata
```

The source archive is an explicit allowlist. It includes `.env.example`, documentation, release
scripts, and the repo-only `.devcontainer` secure-development profile so a source recipient can
reproduce developer checks. The wheel contains runtime `app` files only. Neither archive may
contain `.env`, `.env.save`, `.data`, evidence, development worktrees, Git metadata, caches,
bytecode, links, or local databases. `.dockerignore` keeps the same private paths out of the
Docker build context.

The SPDX file is generated from `uv.lock`, so it describes the universal locked resolution,
including optional and platform-specific packages. It does not claim that every component is
installed on every machine, and unresolved package licensing remains `NOASSERTION`. It does not
describe the secure container's Ubuntu or locked Codex/npm components; the container job records
an image inventory and emits an image SPDX file when the hosted runner provides `docker sbom`.
The local release manifest binds the commit, lock, SBOM, filenames, sizes, and SHA-256 hashes. It
is useful provenance but is not a cryptographic signature.

CI uploads verified candidates for seven days. Downloaded files must be rechecked before use:

```bash
python scripts/verify_release_artifacts.py --dist /absolute/path/to/dist --check-metadata
```

## Non-destructive backup/restore verification

Keep credentials in `DATABASE_URL`; never put the URL in command arguments:

```bash
export DATABASE_URL='postgresql+psycopg://...'
uv run python scripts/verify_postgres_backup_restore.py
```

The command:

- reads the source without changing it;
- writes a mode-`0600` custom archive;
- creates a random `trading_agent_restore_*` database on the same server;
- restores with ownership and privileges omitted;
- compares every public-table row count and Alembic revision;
- force-drops only the random verification database in a `finally` block; and
- removes the temporary archive.

Run while application writes and migrations are paused. If source counts or the Alembic revision
change during the dump, verification fails rather than making a false integrity claim. Use the
separate migration/backup owner role, never the hosted least-privilege runtime role: the role must
see every tenant row and be allowed to create and drop databases. Some managed services,
including restricted Neon roles, do not grant that permission; use an isolated PostgreSQL
verification server in that case.

Retain a verified backup by choosing a new absolute path:

```bash
uv run python scripts/verify_postgres_backup_restore.py \
  --backup-path /absolute/private/path/trading-agent-before-release.dump
```

The path must not already exist. The command never restores over the source database.
PostgreSQL is only part of a complete backup: copy `.data/evidence` separately when retaining
chart evidence, and keep both copies encrypted and outside Git.

## Destructive migration drill for a disposable database

The migration drill is intentionally restricted to database names beginning with
`trading_agent_release_`, and requires the exact name twice:

```bash
export DATABASE_URL='postgresql+psycopg://trading:...@localhost/trading_agent_release_local'
uv run python scripts/run_migration_drill.py \
  --confirm-disposable-database trading_agent_release_local
```

It upgrades to head, performs the isolated backup/restore verification, downgrades the reviewed
reversible tail to `a73f1c9d4e20`, re-upgrades, and runs `alembic check`. Revision
`a73f1c9d4e20` itself is intentionally irreversible because removing account/workspace ownership
would lose information. When another irreversible migration is introduced, update the drill
floor only after explicit migration review.

## Rollback

Rollback is a controlled recovery, not `git reset` and not a blind Alembic downgrade.

1. Stop application/API/watcher processes and pause database writes.
2. Record the failing commit, artifact SHA-256, current Alembic revision, and failure symptoms.
3. Preserve a new post-failure backup and `.data/evidence`; do not overwrite the pre-release
   backup.
4. If the previous application is schema-compatible, install the exact prior reviewed artifact
   by its verified hash and restart against the unchanged database.
5. If the schema is incompatible or the migration is irreversible, create a **new** database,
   restore the verified pre-release archive there, validate row counts/revision/health, and only
   then change `DATABASE_URL`. Keep the failed database untouched for forensics.
6. Re-run `trade health --strict`, account/workspace selection checks, and bounded read-only
   broker/news verification before resuming normal use.
7. Document the incident and add a regression check before another release candidate.

Never downgrade through `a73f1c9d4e20`; never restore into the live source name; never delete the
failed database or pre-release backup until recovery is independently verified.

## External limitations

- macOS and Windows installation are verified by hosted CI runners, not by the local machine.
- Secure-container behavior needs Docker/Linux kernel networking capabilities; static tests run
  locally, while CI performs a credential-free Docker build and runtime checks for the allowed
  OpenAI proxy path, denied arbitrary/proxy-bypass/DNS paths, ephemeral Codex state, absent
  `sudo`, clean Git access, and fail-closed startup when DNS is unavailable.
- A successful hosted Linux runtime check does not prove the runtime firewall on every Docker
  Desktop/kernel combination. Launch-time firewall verification remains mandatory.
- GitHub candidate artifacts are retained temporarily and the manifest is unsigned. A future
  public release should add an explicitly authorized signed tag and Sigstore/GitHub artifact
  attestation after repository release policy is decided.
