"""Turn raw ClinicalTrials.gov records into one tidy table: data/processed/trials.parquet.

One row per trial, with:
- the outcome: terminated (1) or completed (0), and the free-text reason for stopping
- design features known when the trial is registered (phase, sponsor, randomisation, masking, ...)
- therapeutic areas (top-level MeSH disease categories)
- text fields (title, summary, conditions, keywords) for text models
- enrollment, kept only to demonstrate data leakage (it is the *actual* enrollment)
- the reason for stopping, grouped into categories by keyword rules
"""
import gzip
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "trials.jsonl.gz"
OUT = ROOT / "data" / "processed" / "trials.parquet"

# Top-level MeSH disease categories (C and F03 trees) that appear among a condition's ancestors
AREAS = {
    "Neoplasms": "Cancer",
    "Cardiovascular Diseases": "Cardiovascular",
    "Infections": "Infections",
    "Nervous System Diseases": "Neurology",
    "Mental Disorders": "Mental health",
    "Respiratory Tract Diseases": "Respiratory",
    "Digestive System Diseases": "Digestive",
    "Nutritional and Metabolic Diseases": "Metabolic",
    "Endocrine System Diseases": "Endocrine",
    "Immune System Diseases": "Immune",
    "Musculoskeletal Diseases": "Musculoskeletal",
    "Skin and Connective Tissue Diseases": "Skin & connective tissue",
    "Urogenital Diseases": "Urogenital",
    "Female Urogenital Diseases and Pregnancy Complications": "Urogenital",
    "Male Urogenital Diseases": "Urogenital",
    "Eye Diseases": "Eye",
    "Hemic and Lymphatic Diseases": "Blood",
    "Wounds and Injuries": "Injuries",
}

# Reasons for stopping, matched in order against the free-text "why stopped" field.
# A trial can match several categories.
REASONS = {
    "Slow enrollment": r"accru|enrol|recruit|participants? (?:were|was) not|lack of (?:patients|subjects|participants)|insufficient (?:number|patients|subjects)",
    "Business or strategic": (r"business|strateg|commercial|portfolio|sponsor(?:'s)? decision|company(?:'s)? decision|"
                              r"decision (?:by|of) (?:the )?(?:sponsor|company)|sponsor (?:has )?decided|prioriti|"
                              r"development (?:program|plan)|(?:stop|discontinu|halt)\w* (?:the )?(?:clinical )?development|acquisition|merger"),
    "Funding": r"fund|financ|budget|grant|money|\bcost",
    "Lack of efficacy": (r"futil(?!\w* (?:in|of|for) (?:the )?(?:recruit|enrol|accru))|efficac|lack of (?:effect|benefit|response)|did not meet|"
                         r"unlikely to (?:meet|achieve|demonstrate|show)|no (?:significant )?(?:benefit|difference)|insufficient (?:effect|response)"),
    "Safety": (r"safety|adverse|toxicit|side[- ]effect|\bdeaths?\b|\bharm|risk[- /]benefit|benefit[- /]risk|"
               r"unacceptable risk|hepatotox|cardiotox"),
    "Investigator or site": r"\bpi\b|investigator|principal|left the (?:institution|university|hospital)|relocat|site (?:closed|closure)|staff",
    "Drug supply or regulatory": r"supply|drug availab|no longer available|manufactur|regulat|\bfda\b|\birb\b|ethics|license|withdrawn from the market",
    "COVID-19": r"covid|pandemic|coronavirus|sars-cov",
}

# Statements such as "not related to safety or efficacy concerns" must not count as safety or efficacy reasons
NEGATION = re.compile(r"(?:\bnot\b|\bno\b|unrelated to|without|regardless of)(?:\W+\w+){0,5}?\W+(?:safety|efficacy|tolerability)"
                      r"(?:\W+(?:or|and|/|nor)\W+(?:safety|efficacy|tolerability))*")

# Phrases that mention safety without being a safety reason
NOT_A_SAFETY_REASON = re.compile(r"data (?:and )?safety monitoring (?:board|committee)|\bdsmb\b|non-safety|"
                                 r"safety follow-up|\bno (?:serious )?adverse events?|without (?:any )?adverse events?")

PHASE_LABEL = {("PHASE2",): "Phase 2", ("PHASE3",): "Phase 3", ("PHASE1", "PHASE2"): "Phase 1/2",
               ("PHASE2", "PHASE3"): "Phase 2/3"}
SPONSOR_LABEL = {"INDUSTRY": "Industry", "OTHER": "Academic / other", "NIH": "Government", "FED": "Government",
                 "OTHER_GOV": "Government", "NETWORK": "Research network"}


def age_years(text):
    """'18 Years' -> 18.0, '6 Months' -> 0.5; None if missing."""
    if not text:
        return np.nan
    num, unit = text.split()[0], text.split()[1].lower()
    factor = {"year": 1, "years": 1, "month": 1 / 12, "months": 1 / 12, "week": 1 / 52, "weeks": 1 / 52,
              "day": 1 / 365, "days": 1 / 365, "hour": 1 / 8760, "hours": 1 / 8760, "minute": 0, "minutes": 0}
    return float(num) * factor.get(unit, np.nan)


def parse(study):
    p = study["protocolSection"]
    ident, status, design = p["identificationModule"], p["statusModule"], p.get("designModule", {})
    sponsor = p.get("sponsorCollaboratorsModule", {})
    arms = p.get("armsInterventionsModule", {})
    elig = p.get("eligibilityModule", {})
    outcomes = p.get("outcomesModule", {})
    locations = p.get("contactsLocationsModule", {}).get("locations", []) or []
    oversight = p.get("oversightModule", {})
    design_info = design.get("designInfo", {})
    browse = study.get("derivedSection", {}).get("conditionBrowseModule", {})
    mesh_terms = {m["term"] for m in (browse.get("meshes", []) or []) + (browse.get("ancestors", []) or [])}
    collaborators = sponsor.get("collaborators", []) or []
    interventions = arms.get("interventions", []) or []
    criteria = elig.get("eligibilityCriteria", "") or ""
    countries = {loc.get("country") for loc in locations if loc.get("country")}

    row = {
        "nct_id": ident["nctId"],
        "terminated": int(status["overallStatus"] == "TERMINATED"),
        "why_stopped": status.get("whyStopped"),
        "start_year": int(status["startDateStruct"]["date"][:4]),
        "phase": PHASE_LABEL.get(tuple(design.get("phases", [])), "Other"),
        "sponsor": SPONSOR_LABEL.get(sponsor.get("leadSponsor", {}).get("class"), "Other"),
        "n_collaborators": len(collaborators),
        "industry_collaborator": int(any(c.get("class") == "INDUSTRY" for c in collaborators)),
        "allocation": design_info.get("allocation", "NA"),
        "intervention_model": design_info.get("interventionModel", "UNKNOWN"),
        "purpose": design_info.get("primaryPurpose", "UNKNOWN"),
        "masking": design_info.get("maskingInfo", {}).get("masking", "UNKNOWN"),
        "n_arms": len(arms.get("armGroups", []) or []),
        "n_interventions": len(interventions),
        "n_primary_outcomes": len(outcomes.get("primaryOutcomes", []) or []),
        "n_secondary_outcomes": len(outcomes.get("secondaryOutcomes", []) or []),
        "healthy_volunteers": int(bool(elig.get("healthyVolunteers"))),
        "sex": elig.get("sex", "ALL"),
        "min_age": age_years(elig.get("minimumAge")),
        "max_age": age_years(elig.get("maximumAge")),
        "criteria_words": len(criteria.split()),
        "n_locations": len(locations),
        "n_countries": len(countries),
        "us_site": int("United States" in countries),
        "has_dmc": {True: "Yes", False: "No"}.get(oversight.get("oversightHasDmc"), "Not reported"),
        "n_conditions": len(p.get("conditionsModule", {}).get("conditions", []) or []),
        "title": ident.get("officialTitle") or ident.get("briefTitle", ""),
        "summary": p.get("descriptionModule", {}).get("briefSummary", ""),
        "conditions": "; ".join(p.get("conditionsModule", {}).get("conditions", []) or []),
        "keywords": "; ".join(p.get("conditionsModule", {}).get("keywords", []) or []),
        "enrollment": design.get("enrollmentInfo", {}).get("count"),
        "enrollment_type": design.get("enrollmentInfo", {}).get("type"),
    }
    for t in ["DRUG", "BIOLOGICAL", "DEVICE", "PROCEDURE", "BEHAVIORAL", "RADIATION", "DIETARY_SUPPLEMENT"]:
        row[f"iv_{t.lower()}"] = int(any(i.get("type") == t for i in interventions))
    areas = {AREAS[t] for t in mesh_terms if t in AREAS}
    for area in sorted(set(AREAS.values())):
        row[f"area_{area}"] = int(area in areas)
    return row


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(RAW, "rt", encoding="utf-8") as f:
        df = pd.DataFrame(parse(json.loads(line)) for line in f)

    reason_text = df["why_stopped"].fillna("").str.lower().str.replace(NEGATION, " ", regex=True)
    reason_text = reason_text.str.replace(NOT_A_SAFETY_REASON, " ", regex=True)
    for name, pattern in REASONS.items():
        df[f"reason_{name}"] = (reason_text.str.contains(pattern, regex=True) & (df["terminated"] == 1)).astype(int)
    reason_cols = [f"reason_{n}" for n in REASONS]
    df["reason_Other or unclear"] = ((df[reason_cols].sum(axis=1) == 0) & (df["terminated"] == 1)).astype(int)

    df.to_parquet(OUT, index=False)
    print(f"{len(df):,} trials, {df['terminated'].mean():.1%} terminated -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
