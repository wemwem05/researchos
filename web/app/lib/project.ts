// Research Project state mirror (prototype/project.py) + pure view helpers.

export type ConnectionType =
  | "supports"
  | "contradicts"
  | "extends"
  | "overlaps_with"
  | "uses_same_method"
  | "uses_different_assumption"
  | "leaves_open";

export const CONNECTION_TYPES: ConnectionType[] = [
  "supports",
  "contradicts",
  "extends",
  "overlaps_with",
  "uses_same_method",
  "uses_different_assumption",
  "leaves_open",
];

export interface PaperEntry {
  paper_id: string;
  title: string;
  filename: string;
  added_order: number;
  n_objects: number;
  n_localized: number;
  web_pdf: string | null;
  web_data: string | null;
}

export interface Finding {
  id: string;
  paper_id: string;
  statement: string;
  evidence: string;
  page: number | null;
  object_ids: string[];
  confidence: number;
}

export interface Connection {
  id: string;
  from_ref: string;
  to_ref: string;
  type: ConnectionType;
  reason: string;
  confidence: number;
}

export interface OpenQuestion {
  id: string;
  text: string;
  raised_by: string;
  finding_refs: string[];
}

export interface NoveltyRisk {
  id: string;
  direction: string;
  overlapping_finding_refs: string[];
  note: string;
  raised_by: string;
}

export interface PaperContribution {
  paper_id: string;
  what_it_adds: string;
  stance_toward_prior: string;
  overlap_with_direction: string;
  new_open_questions: string[];
}

export interface ProjectData {
  schema_version: string;
  research_question: string;
  papers: PaperEntry[];
  findings: Finding[];
  connections: Connection[];
  contributions: PaperContribution[];
  open_questions: OpenQuestion[];
  novelty_risks: NoveltyRisk[];
}

/** Fixed corpus scope prefix: novelty is never claimed globally. */
export const CORPUS_SCOPE_PREFIX = "Within the papers currently analyzed,";

export function paperById(project: ProjectData, paperId: string): PaperEntry | undefined {
  return project.papers.find((p) => p.paper_id === paperId);
}

export function findingById(project: ProjectData, id: string): Finding | undefined {
  return project.findings.find((f) => f.id === id);
}

/** Human label for a connection endpoint: finding id or paper title. */
export function endpointLabel(project: ProjectData, ref: string): string {
  if (ref.startsWith("paper:")) {
    return paperById(project, ref.slice(6))?.title ?? ref;
  }
  const f = findingById(project, ref);
  if (!f) return ref;
  const paper = paperById(project, f.paper_id);
  const short = paper ? paper.title.split(":")[0] : f.paper_id;
  return `${f.id} · ${short}`;
}

export function groupConnectionsByType(
  connections: Connection[]
): Map<ConnectionType, Connection[]> {
  const m = new Map<ConnectionType, Connection[]>();
  for (const c of connections) {
    const list = m.get(c.type) ?? [];
    list.push(c);
    m.set(c.type, list);
  }
  return m;
}

export function findingsForPaper(project: ProjectData, paperId: string): Finding[] {
  return project.findings.filter((f) => f.paper_id === paperId);
}

/** Reader deep link for a finding: paper reader + first grounded object. */
export function findingReaderLink(project: ProjectData, finding: Finding): string {
  const obj = finding.object_ids[0];
  return `/read/${finding.paper_id}${obj ? `?select=${encodeURIComponent(obj)}` : ""}`;
}
