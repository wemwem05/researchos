# ResearchOS

A research-question-aware scientific paper workspace. (Meta AI hackathon project.)

## Milestone 1: minimal technical prototype

Tests one assumption: given a scientific PDF + a research question, Muse
(`muse-spark-1.3` via the Meta Model API) can produce structured, grounded JSON
describing the paper's important scientific objects — equations, figures,
tables, and experimentally testable claims.

This is a prototype, not a production app. No auth, databases, frontend,
literature search, or vector DB yet.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r prototype/requirements.txt
cp .env.example .env   # then put your key in .env (never commit it)
```

The only secret is `MODEL_API_KEY`, read from the environment (a local `.env`
file works too). See `.env.example`.

## Run

```bash
source .venv/bin/activate
python prototype/analyze_paper.py \
  --pdf samples/eot_shape_correspondence.pdf \
  --question "Can alternative optimal transport costs improve non-rigid shape correspondence?"
```

Output: `prototype/output.json` (validated against Pydantic models before writing).

Options: `--output PATH`, `--model ID` (default `muse-spark-1.3`),
`--base-url URL` (default `https://api.meta.ai/v1`).

Notes:

- The PDF is sent directly to the model (base64 `input_file`); no text
  extraction step. If the Responses API shape is rejected, the script falls
  back to Chat Completions with `json_schema`, then plain-JSON Chat
  Completions. All paths are validated with the same Pydantic models.
- `samples/eot_shape_correspondence.pdf` is a symlink to the full CVPR 2024
  paper file in the same folder, so the documented command works as-is.
- The prompt forbids inventing evidence: missing pages/labels/numbers come back
  as `null` with lowered confidence, and grounding gaps are printed as warnings.

## Layout

- `prototype/analyze_paper.py` — the whole prototype: CLI, prompt, API calls,
  Pydantic models, validation
- `prototype/requirements.txt` — `openai`, `pydantic`, `python-dotenv`
- `samples/` — sample paper PDF
- `HACKATHON_LOG.md` — milestone log
