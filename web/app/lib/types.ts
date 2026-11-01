// Mirror of the prototype/localized_output.json fixture (subset the reader needs).

export type ObjectType = "equation" | "figure" | "table" | "claim";

export interface BoundingBox {
  x_min: number;
  y_min: number;
  x_max: number;
  y_max: number;
}

export interface SymbolMeaning {
  symbol: string;
  meaning: string;
}

export interface EquationDetails {
  latex: string | null;
  symbols: SymbolMeaning[];
  role_in_method: string | null;
}

export interface FigureDetails {
  components: string[];
  flow: string[];
  role_in_method: string | null;
}

export interface TableDetails {
  metric: string | null;
  higher_or_lower_is_better: "higher" | "lower" | null;
  important_comparisons: string[];
  result_interpretation: string | null;
}

export interface SciObject {
  id: string;
  type: ObjectType;
  page: number | null;
  label: string | null;
  title: string;
  what_it_is: string;
  why_it_is_here: string;
  claim_supported: string;
  limitations: string;
  research_relevance: string;
  related_objects: string[];
  confidence: number;
  evidence: string;
  equation: EquationDetails | null;
  figure: FigureDetails | null;
  table: TableDetails | null;
}

export interface Localization {
  object_id: string;
  page: number;
  bbox: BoundingBox | null;
  confidence: number;
  localization_method: string | null;
  localization_note: string | null;
}

export interface LocalizedData {
  analysis: {
    paper_title: string;
    research_question: string;
    objects: SciObject[];
  };
  localizations: Localization[];
}
