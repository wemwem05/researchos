# ResearchOS Hackathon Log

## Milestone 1 — Minimal technical prototype (2026-11-01)

**Goal:** test one assumption — given a scientific PDF + a research question,
Muse can produce structured, grounded JSON describing the paper's important
scientific objects (equations, figures, tables, experimentally testable claims).

**Built:**

- `prototype/analyze_paper.py` — CLI that sends the PDF directly to
  `muse-spark-1.3` (Meta Model API, `https://api.meta.ai/v1`) with the research
  question, requests structured output (`json_schema` from Pydantic models),
  validates the response with Pydantic, and writes `prototype/output.json`.
  Fallback chain: Responses API → Chat Completions + `json_schema` → Chat
  Completions plain JSON. Prompt forbids inventing evidence; grounding gaps
  (missing page, unknown `related_objects` id, missing per-type details) are
  reported as warnings.
- `prototype/requirements.txt` — `openai`, `pydantic`, `python-dotenv`.
- `.env.example` + `.gitignore` — key only via `MODEL_API_KEY`; `.env`,
  `.venv/`, and `prototype/output.json` never committed.
- `README.md` — setup + run instructions.
- `samples/eot_shape_correspondence.pdf` — symlink to the CVPR 2024 EOT +
  functional maps paper, so the documented command works as-is.

**Validation (no API key in this environment):** compile check, `--help`,
clean missing-key error, Pydantic validation of a representative fixture
(incl. rejection of bad `type` / out-of-range `confidence`), JSON-schema
generation for the API request, and `extract_json` fence-stripping. Live
end-to-end run pending `MODEL_API_KEY`.

**Explicitly out of scope:** auth, databases, Next.js, Modal, literature
search, vector DBs, and other product features.
