# Notes store: durable team history and live worktree sharing

## Decision scope

This is a design recommendation, not an implementation plan.  It addresses two
different needs that the current notes store happens to serve:

* a single, immediately shared notes directory for every worktree in one clone;
  and
* durable, recoverable, team-shareable institutional memory.

They should not be made the same filesystem path.  Today
`.lanegate/notes/` is intentionally gitignored and each worktree symlinks that
path to the control checkout's live directory.  That makes a note written in
one worktree instantly visible in the others on that machine, and also keeps
the notes alive when an executor worktree is removed.

The guard in `lanegate/worktree.py` is material to this decision: it checks
whether `.lanegate/notes` is ignored before creating the symlink.  If the path
is not ignored, it refuses to shadow a tracked path and falls back to a private
worktree-local notes directory, with a warning.  Simply removing the ignore
rule would therefore turn the intended shared live store into quietly divergent
stores.  Any direction that tracks notes must retain an ignored live path (or
replace the worktree mechanism deliberately); it must not rely on unignoring
the current path.

## Candidate A: periodic or triggered commits

Keep `.lanegate/notes/` as the ignored, control-checkout-backed live store and
periodically copy a curated snapshot into a separate tracked location, then
commit it.  Useful triggers include a successful ticket merge, an explicit
maintenance command, or a scheduled job.

Benefits:

* The existing symlink continues to provide immediate cross-worktree sharing.
* Committed snapshots are recoverable through Git and can be reviewed, pushed,
  and pulled by a team.
* A snapshot boundary can exclude transient or malformed notes and avoids
  committing after every small write.

Costs and risks:

* A note can be lost between snapshots unless the trigger is frequent and its
  failure is visible.
* Snapshot copying needs conflict and deletion rules; otherwise a later copy
  can overwrite an intended remote edit.
* A background commit changes a developer's checkout and needs clear ownership,
  authentication, and failure handling.  Committing directly from arbitrary
  executor worktrees would also create avoidable concurrent-write races.

This direction supplies durability, but by itself does not make other machines'
notes appear locally.  It still needs an import/reconciliation behavior after a
pull, so it is incomplete as the whole team-sharing design.

## Candidate B: explicit sync and pull reconciliation

Use two stores with separate roles: retain ignored `.lanegate/notes/` as the
live per-clone store, and maintain a distinct Git-tracked canonical notes
location (or dedicated notes branch) for exchange between clones.  A deliberate
`sync` operation publishes local curated changes and imports changes obtained
by Git pull into the live store.  It should report conflicts rather than choose
a winner silently.

Benefits:

* It keeps the current live symlink mechanism intact: all local worktrees read
  and write the same ignored path.
* Git gives team sharing, reviewable history, ordinary backup/restore, and a
  way to recover notes removed by cleanup.
* The command boundary gives users a visible time to resolve concurrent edits,
  validate note format, and decide whether deletions propagate.
* The tracked and live paths are distinct, so the worktree symlink guard is not
  bypassed or weakened.

Costs and risks:

* Team sharing is not instantaneous; users or automation must run sync after
  pull and before publishing useful local knowledge.
* Reconciliation needs a stable note identity, conflict policy, and clear
  behavior for renames and deletions.
* Maintaining a local live cache plus canonical tracked state adds operational
  complexity compared with one directory.

## Candidate C: non-Git shared service or network location

Continue using a shared filesystem location, or introduce an append-only log
or small service as the team source of truth.  Worktrees could still symlink to
a local cache or the shared location.

Benefits:

* It can offer near-real-time sharing across machines and avoid Git merge
  conflicts for frequently edited notes.
* A service can provide append-only history, access control, retention, and
  search tailored to notes rather than source files.

Costs and risks:

* It adds infrastructure, credentials, availability, backup, and migration
  responsibilities to a lightweight developer workflow.
* A shared mount alone is not automatically durable: it still requires versioned
  backups and recovery testing, and it is less naturally reviewable in normal
  repository workflows.
* Offline work and public/open-source contributors become harder unless a local
  replication model is designed as well.

## Recommendation

Adopt Candidate B: an explicit Git sync/pull reconciliation design with a
separate, tracked canonical notes store and the existing ignored live
`.lanegate/notes/` store.  It is the smallest direction that meets both goals:
Git makes the curated history durable, recoverable, and shareable with a team,
while the unchanged live path preserves immediate sharing among worktrees of
one clone.  Candidate A can later provide optional automation for Candidate B
(for example, a merge-time reminder or scheduled sync), but should not be the
only mechanism because commits alone do not reconcile another clone's notes.
Candidate C should be reconsidered only if note volume, collaboration latency,
or access-control requirements demonstrate that Git reconciliation is no longer
adequate.

The follow-up implementation proposal should specify the tracked location, the
one-way/bootstrap rules, conflict and deletion semantics, atomic update
behavior for the live store, and a visible failure status.  It should also test
the negative boundary: if `.lanegate/notes` ceases to be ignored, setup must not
pretend that live cross-worktree sharing still works.

## Product scope

This should be evaluated as a LaneGate product capability, not silently enabled
only in this repository.  The mechanism exists to support agents in any
initialized repository, and teams need an explicit, documented choice about
where institutional memory is stored.  The default shipped by `lane init`
should preserve the current ignored live notes behavior until the sync feature
is implemented and proven.  A later product decision can offer opt-in setup for
the tracked canonical store; it must not remove the ignore rule merely to make
notes appear Git-backed.

## Non-goals

This document does not change `.gitignore`, alter `lanegate/worktree.py`, add a
sync command, or migrate existing notes.  Those are implementation work for a
later ticket after the storage layout and reconciliation contract are approved.
