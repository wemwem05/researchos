# ResearchOS — Final Repository & Submission Audit

Date: 2026-11-04 (audited against local HEAD `0d22284`, deadline Nov 4 9PM ET).
Method: independent read-only inspection (`git status/log/ls-files/rev-list/grep`,
targeted secret scans, blob-size analysis, full test/build validation).
No history rewrite, no commits, no pushes, no secret values reproduced here.

## A. EXECUTIVE RESULT

**READY WITH WARNINGS** — safe to publish after the ordered actions in §L.

- Current tree: no secrets, no tracked PDFs, no tracked `node_modules`/build output.
- Reachable history: no secrets found, but contains one 3.5 MB published paper PDF
  and full `web/node_modules/` binaries (~86 MB largest). No action can remove
  this without history rewrite (explicitly NOT done); exposure is documented below.
- Fresh clone works for code + JSON demo evidence; local-only PDFs must be
  re-supplied by the judge (reproducibility plan in §G).
- Tests/builds all green in this session (§J).

## B. CURRENT GIT STATE

- Branch: `main`; remote: `origin https://github.com/wemwem05/researchos.git`.
- HEAD `0d22284` ("milestone 9") == `origin/main`; `git ls-remote` matches;
  ahead/behind `0 0`; upstream `origin/main`. Nothing committed-but-unpushed.
- Staged changes: none (`git diff --cached` empty).
- Unstaged modifications (21): `.gitignore`, `README.md`, `HACKATHON_LOG.md`,
  `docs/CURRENT_STATE.md`, 15 `web/app/**` redesign files, `web/app/globals.css`,
  `web/app/layout.tsx`, `web/app/page.tsx`.
- Untracked (13, all should be committed): `.env.example`, `LICENSE`,
  `docs/SUBMISSION.md`, `docs/DEMO_SCRIPT.md`, 9 new `web/app/components/**`
  files (`AppShell, Sidebar, ui, cards, OverviewTab, PapersTab, EvidenceTab,
  ActivityTab, ResearchQuestionPanel`).
- `git ls-files web/node_modules` → 0 files (not tracked in current tree).
- `git ls-files "*.pdf"` → empty (no PDFs tracked now).
- `.git/info/exclude`: empty (no local-only excludes hiding anything).
- No stash entries. `.env` not tracked; no `.env` in history (see §C).

## C. CRITICAL SECURITY FINDINGS

Current tree (targeted `git grep` scans over tracked files):
- No `MODEL_API_KEY`/`BASETEN_API_KEY`/`MODAL_TOKEN_*`/OpenAlex assignments,
  no private keys, no bearer tokens, no webhook secrets.
- `.env.example` contains 5 empty/commented keys only (verified, no values).
- Tracked JSON fixtures: no key-like values (`(baseten|modal|api[_-]?key)
  ["'](sk-|[A-Za-z0-9]{20,})` → no matches outside node_modules).
- Absolute local paths: only `web/public/pdf.worker.min.mjs` (standard pdf.js
  dist build paths, LOW, not user data). No `/Users/madi` in fixtures.
- Telemetry in tracked fixtures contains provider names, request IDs, token
  counts, latencies — no credentials, no request headers.

Reachable history (`git grep` over all commits + `git log -S` spot checks):
- No `.env` file ever committed. No key assignments ever committed.
- Only `PRIVATE KEY` matches are binary blobs under `web/node_modules/`
  (commit `7cb1b55`), i.e. dependency files, not project secrets.
- Secret-scanner caveat: Gitleaks is not installed; scans used fixed-pattern
  `git grep` (`API_KEY\s*=\s*["'][^"']+`, `TOKEN_SECRET`, `BEGIN .*PRIVATE KEY`,
  `Bearer`, etc.) across tree + history. No hits with values.

Severity: no BLOCKER/HIGH. No revocation/rotation required by this audit.

## D. ACCIDENTAL TRACKED FILES

None in the current tree:
- `web/node_modules/`: 0 tracked files (history only, see §F).
- `web/.next/`, `.venv/`, `__pycache__/`, `*.pyc`: untracked + ignored.
- `prototype/projects/`, `prototype/output*.json`, `loc_cache.json`,
  `debug/`: untracked + ignored (local scientific state stays local).
- `web/public/pdf.worker.min.mjs` (1.2 MB) is intentionally tracked AND
  produced by `postinstall` — harmless duplication (LOW): builds work with
  or without it; risk is only a stale worker after a pdf.js upgrade.
- `dev.sh` mode `100755` preserved (executable bit intact).

## E. MISSING ESSENTIAL FILES

Everything essential is present locally; the gap is that 13 files are
**uncommitted** (§B) — the public repo is incomplete until they are pushed:
- `LICENSE` (MIT, untracked) — submission should include the license.
- `.env.example` (untracked) — judges need it for setup; missing
  `OPENALEX_MAILTO` (fixed in §M: code reads `OPENALEX_MAILTO` in
  `prototype/openalex.py:40`, docs never mentioned it).
- `docs/SUBMISSION.md`, `docs/DEMO_SCRIPT.md` (untracked) — required by M9.
- 9 new frontend components (untracked) — imported by modified pages;
  pushing pages without them would break the build.
- No code refers to absent committed files (fixtures under `web/public/data/`
  all tracked; legacy `/read/*` + project routes resolve to tracked JSON).
- `samples/` is local-only by design (see §G), but README CLI examples use
  `samples/eot_shape_correspondence.pdf` — reproducibility note added (§M).

## F. HISTORICAL PDFs / LICENSE RISKS

Verified with `git rev-list --objects` + `git cat-file -s` (full hashes):

1. **One published paper in reachable history** (MEDIUM, license/redistribution):
   - Blob `45483d6f…` (3,689,723 bytes):
     `samples/Le_Integrating_Efficient_Optimal_Transport_and_Functional_Maps_
     For_Unsupervised_Shape_CVPR_2024_paper.pdf`
   - Added in `32836a8` ("milestone 4"), deleted from tracking in `4c21edf`.
     Still reachable from `main` via ancestors → every clone downloads it.
   - This is a CVPR 2024 published paper. Public availability ≠ redistribution
     right. Decision needed: leave in history (simplest; common for hackathons
     but not license-clean) or history rewrite (destructive, NOT done here).
2. **105-byte symlink** `samples/eot_shape_correspondence.pdf` → the CVPR file
   (same commits; harmless itself, points at the same blob).
3. **`web/public/papers/` was NEVER tracked** (`git log --diff-filter=A` empty):
   deep-shells PDF was never committed — exposure is smaller than feared.
   Local `web/public/paper.pdf` + `web/public/papers/*.pdf` are git-ignored.
4. **`web/node_modules/` in history** (MEDIUM, clone weight, no license issue):
   reachable from `main` (commit `7cb1b55`): 90 MB `swc-darwin-arm64.node`,
   30 MB `skia` binary, 18 MB libvips, etc. Pack is 78.88 MiB / `.git` 82 MB.
   Every fresh clone pays this cost permanently unless history is rewritten.
5. No videos, ZIPs, datasets, or virtualenvs in history. Only other >1 MB
   non-`node_modules` blob on `main` is the intentional `pdf.worker.min.mjs`.

No action taken (read-only audit). Recommendation: accept + disclose, do not
rewrite history this close to the deadline (§L).

## G. CLONE-AND-RUN REPRODUCIBILITY

Fresh-clone inventory (judge has code + tracked files only):

Works without keys or local PDFs:
- `./dev.sh` starts backend (127.0.0.1:8765) + frontend (:3000).
- Projects home, new empty project, question editing, job/progress UI.
- Tracked JSON demo evidence: `web/public/data/agent/**` (audits, runs,
  `demo_project.json`), localized-output fixtures — audits, Modal reports,
  agent runs, cross-paper connections all inspectable model-independently.
- Legacy `/read/*` routes render overlays from tracked JSON.

Requires judge-supplied PDFs (local-only, git-ignored by design):
- `samples/*.pdf` (3.7 MB CVPR EOT + 5.1 MB NeurIPS deep-shells + symlink —
  three local copies share MD5 `50015f73…`; deep-shells never committed).
- `web/public/paper.pdf`, `web/public/papers/*.pdf` — required by staged
  `web_pdf: /papers/…` fixtures; on a fresh clone the Reader shows overlays
  without the underlying PDF pages (graceful but degraded).
- Reproducibility plan (§M + §L): README "Demo data & PDFs" section with
  source links (CVPR open access, NeurIPS proceedings), exact expected
  filenames, and the manual-upload path (M7 browser upload needs no CLI).

Requires API keys (documented in `.env.example`):
- `MODEL_API_KEY` — PDF analysis/localization (Meta Model API).
- `BASETEN_API_KEY` — all agentic reasoning (Spark Open, §H).
- `MODAL_TOKEN_ID/SECRET` or `modal token new` — computational verification;
  app shows "unavailable" without them, never fakes results.
- `OPENALEX_API_KEY` optional (discovery works keyless); `OPENALEX_MAILTO`
  recommended for the polite pool (now documented).

No hardcoded absolute paths, no private-state assumptions in tracked code.

## H. META OPEN-WEIGHT VERIFICATION

- Exact model ID in code: `prototype/spark_open.py:36`
  `MODEL_ID = "meta-models/muse-spark-open-1026"`, `BASE_URL =
  "https://inference.baseten.co/v1"` (OpenAI-compatible client). No fallback
  model anywhere on the agent path.
- Consumers (all via `SparkOpen` client): M5 investigator/critic
  (`agent_graph.py`), M6.1 audit graph, M8 discovery graph
  (`discovery_graph.py`), verification planning. PDF analysis/localization
  honestly use Meta-hosted `muse-spark-1.3` via `MODEL_API_KEY` — docs
  distinguish the two paths (`docs/OPEN_WEIGHT_USAGE.md`, README:164).
- Live-inference proof in tracked fixtures: staged runs record
  `provider: Baseten`, real latencies (e.g. 7827 ms), request IDs, token
  counts — not mocks. Telemetry plumbing in `spark_open.py` + run records.
- Docs: `OPEN_WEIGHT_USAGE.md` + `SUBMISSION.md` name the exact model ID
  and Baseten; no claim that every inference uses open weights.

## I. AGENTIC / MODAL VERIFICATION

- LangGraph: `agent_graph.py` (investigator/critic + `interrupt()` HITL),
  discovery graph (`load_context → … → critic → persist`), verify graph;
  `SqliteSaver.from_conn_string` checkpointing; per-project thread routing.
- Real Modal proof in tracked `verifications.json`: sandbox IDs `sb-01M6…`,
  durations 595 s / 247 s, measured IQRs, artifacts; honest
  `not_testable` / `execution_failed` verdicts (not hidden, not relabeled).
- Bounded revision (≤1 critic revision), deterministic validators, retry/
  failure behavior in `modal_exec.py`; docs never present mock executions
  as independent verification.

## J. TEST AND BUILD RESULTS

Run in this session (no model/Modal calls; all green):
- Backend: `295 tests … OK` (`unittest discover -s prototype`, 7.9 s).
- Frontend: `50 passed (50)` across 7 vitest files.
- `npx tsc --noEmit`: clean.
- `npx next build --webpack`: "Compiled successfully", 4/4 static pages.
- `dev.sh` executable bit `100755` verified; `SERVER_VERSION`/`EXPECTED_
  SERVER_VERSION` both `8.0`.
- Live browser verification NOT possible in this sandbox (port binding
  forbidden) — manual 1440px/390px screenshot pass still owed by the user;
  10/10 in-process route checks passed earlier (`/tmp/m9_review.py`).

## K. SUBMISSION DOCUMENTATION GAPS

- `docs/SUBMISSION.md:115`: `Demo video: TODO` — user must add the link
  (MEDIUM; cannot be filled by audit).
- Portal-required fields must be verified by the user, not inferred (AGENTS.md).
- No deployment claimed (correct — local-only by design, bound to
  127.0.0.1, documented production limits).
- README stale secret claim (fixed §M: "The only secret is MODEL_API_KEY").
- README `public/paper.pdf` file-tree entry now annotated local-only (§M).
- Screenshots: none committed; optional, not blocking.

## L. REQUIRED ACTIONS, ORDERED BY SEVERITY

1. **HIGH — Commit + push the 34-file working tree.** The public repo currently
   lacks the redesign pages' imports, license, env example, and submission
   docs; pushing only part of it breaks the build. Safe, non-destructive.
   Exact commands in §N.
2. **HIGH — Manual browser pass (1440px + 390px)** over Projects, Overview,
   Papers, Discover, Evidence, Activity, Reader+equation, Reader+claim.
   Sandbox cannot bind ports, so this was never visually verified. Record
   screenshots; fix overflow/stacking issues if found.
3. **MEDIUM — Add the demo-video URL** to `docs/SUBMISSION.md:115` (replace
   TODO), then amend/push. Do not submit with a TODO.
4. **MEDIUM — Decide on the historical CVPR PDF** (§F.1): recommended =
   leave history untouched, disclose in submission Q&A if asked. Rewrite
   (`git filter-repo`, force-push, key rotation review) is destructive and
   risks the deadline — NOT recommended tonight.
5. **MEDIUM — Accept the 79 MB clone weight** from historical node_modules
   (§F.4): same recommendation — disclose, don't rewrite.
6. **LOW — Post-submission hygiene**: `pdf.worker.min.mjs` tracked-vs-
   postinstall duplication; three identical local EOT copies (keep one +
   symlink); consider `git filter-repo` + `filter-lfs` only after judging.

## M. SAFE FIXES APPLIED BY THIS AUDIT

Non-destructive doc/config edits only (no commits, no deletions):
- `README.md`: "The only secret is MODEL_API_KEY" → full secret list
  (`MODEL_API_KEY`, `BASETEN_API_KEY`, `MODAL_TOKEN_*`, `OPENALEX_API_KEY`)
  pointing at `.env.example`.
- `README.md`: `public/paper.pdf` tree entry annotated "(local-only, git-ignored)".
- `README.md`: new "Demo data & PDFs on a fresh clone" subsection (source
  links, expected filenames, manual-upload path).
- `.env.example`: added `OPENALEX_MAILTO=` (+ commented optional
  `RESEARCHOS_MAX_PDF_MB/PAGES` matching `paper_pipeline.py` defaults 32/60).
- Created this file. Nothing else touched; no files deleted.

## N. NEXT GIT COMMANDS (for the user to run)

```bash
git status --short          # expect 21 M + 14 ?? (incl. this audit)
git diff --stat             # review redesign + doc changes
git add -A
git commit -m "final submission: redesign, docs, license, env example, repo audit"
git push origin main
git ls-remote origin main   # confirm remote == local HEAD
```
