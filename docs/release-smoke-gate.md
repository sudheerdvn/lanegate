# Release smoke gate

Run the shipped-artifact smoke gate before a release, or whenever packaging, CLI dependencies, lifecycle setup, or promotion changes:

```bash
python -m pip install build
python ci/smoke_release.py
```

The command copies the checkout to a temporary directory, builds a wheel and sdist there, and installs the wheel into a newly-created virtual environment. It also puts its temporary Git repositories and `HOME` there. It never creates `build/`, `dist/`, or `.lanegate/` in the checkout from which it is run.

The seven checks are:

1. Build a wheel and source distribution.
2. Install the wheel non-editably into an isolated virtual environment.
3. Exercise `lanegate mcp --help`, `lanegate api --help`, and import both entrypoint modules.
4. Import every shipped module without optional extras, and exercise the no-`requests` Ollama fallback.
5. Run `init`, `create`, `open`, `start`, `complete`, approved `review`, and `merge` in a throwaway Git repository.
6. Assert a first manual promotion sees a missing environment branch as pending, creates that branch, carries `main`'s commit, and makes `board` show a later source commit as pending.
7. Run `init -i` with `guard_script: [python, guard.py]`, then run `board` to prove later commands can reload the configuration.

Every failed check is printed by name and makes the command return non-zero. The build failure intentionally prevents checks 2–7 from running, because no wheel exists to exercise. This is how force-include drift is reported without masking the cause.

## Current CI status

The `release-smoke` GitHub Actions job is required on every push and pull request. Check 6 enforces that a first promotion bootstraps its missing environment branch before evaluating pending commits, and that board JSON continues to report later source commits as pending.

The gate is expected to complete well below five minutes on a warmed CI runner. It creates just one wheel-install environment and two tiny Git repositories.
