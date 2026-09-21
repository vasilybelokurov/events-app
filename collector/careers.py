"""Map event text onto career themes.

The point of the site is breadth of *occupations*, not school subjects, so each
event is tagged with the kinds of work it puts on display.  The mapping is
deliberately simple and auditable: a keyword list per theme, matched on word
boundaries against title + summary + source topic labels.
"""

from __future__ import annotations

import re

CAREER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Medicine & health": (
        "medicine", "medical", "surgery", "surgeon", "nhs", "nursing",
        "nurse", "anatomy", "pharmacy", "public health", "epidemic", "cancer",
        "gp surgery", "clinician", "clinical",
        "immune", "vaccine", "mental health", "human body", "anaesthe",
    ),
    "Engineering": (
        "engineering", "engineer", "mechanical", "civil engineer", "robot", "turbine",
        "manufactur", "steam", "aerospace", "structural", "welding", "bridge",
    ),
    "Physics & space": (
        "physics", "astronom", "space", "telescope", "cosmos", "quantum",
        "particle", "gravitational", "fusion", "universe", "cern", "nuclear",
    ),
    "Chemistry & materials": (
        "chemistry", "chemical", "materials", "molecule", "battery", "batteries",
        "polymer", "catalys", "periodic",
    ),
    "Biology, ecology & vets": (
        "biology", "ecolog", "zoolog", "botan", "wildlife", "conservation",
        "veterinar", "animal", "species", "evolution", "genetic", "plants",
        "plant science", "botanical",
        "dinosaur", "palaeo", "paleo", "fossil", "bird", "insect", "microb",
    ),
    "Maths, data & statistics": (
        "mathematic", "maths", "statistic", "probabilit", "data science",
        "modelling", "algebra", "geometry", "numbers",
    ),
    "Computing & AI": (
        "computing", "computer", "software", "coding", "programming",
        "artificial intelligence", "ai", "machine learning", "algorithm",
        "cyber", "digital", "sensor", "wearable",
    ),
    "Law & justice": (
        "law", "legal", "courtroom", "crown court", "magistrat", "barrister",
        "judge", "justice", "trial",
        "supreme court", "criminal", "human rights", "advocacy",
    ),
    "Finance & economics": (
        "economic", "economy", "finance", "financial", "banking", "bank of england",
        "money", "monetary", "investment", "accounting", "inflation",
    ),
    "Design, making & architecture": (
        "design", "architect", "craft", "prototyp", "model-making", "textile",
        "ceramic", "clay", "furniture", "product design", "3d printing",
    ),
    "Film, animation & media": (
        "film", "animation", "animator", "stop-motion", "cinematograph",
        "broadcast", "television", "puppet", "visual effects", "photography",
        "photographer",
    ),
    "Writing & communication": (
        "journalis", "writing", "writer", "science communication", "publishing",
        "storytelling", "podcast", "presenter", "curator", "curating",
    ),
    "Environment & climate": (
        "climate", "environment", "sustainab", "renewable", "net zero",
        "green econom", "energy", "air quality", "ocean", "weather",
    ),
    "History & archaeology": (
        "history", "historian", "archaeolog", "heritage", "museum", "ancient",
        "archive", "antiquar",
    ),
    "Psychology & neuroscience": (
        "psycholog", "neuro", "brain", "behaviour", "cognitive", "consciousness",
    ),
    "Business & entrepreneurship": (
        "business", "entrepreneur", "start-up", "startup", "enterprise",
        "marketing", "management",
    ),
    "Arts & performance": (
        "art", "arts", "music", "dance", "performance", "art gallery",
        "sculpture", "painting", "stage design", "drama", "opera house", "concert", "orchestra",
        "theatre production", "theatre design",
    ),
    "Teaching & outreach": (
        "teaching", "teacher", "education", "outreach", "workshop for schools",
        "careers", "apprenticeship",
    ),
    "Politics & public service": (
        "politic", "government", "policy", "diplomac", "parliament",
        "civil service", "public service",
    ),
}


def infer_careers(*texts: str | None) -> list[str]:
    """Return the career themes implied by the given texts, sorted by name.

    >>> infer_careers("The Operating Theatre: 250 Years of Surgery")
    ['Medicine & health']
    >>> infer_careers("Stage Design Lab", "light, sound and theatre design")
    ['Arts & performance', 'Design, making & architecture']
    >>> infer_careers(None, "")
    []
    """
    blob = " " + re.sub(r"\s+", " ", " ".join(t for t in texts if t)).lower() + " "
    hits = set()
    for theme, words in CAREER_KEYWORDS.items():
        for w in words:
            # Short keywords ("ai", "art", "law") must match a whole word;
            # longer ones may match a stem ("engineer" -> "engineering").
            pattern = rf"\b{re.escape(w)}\b" if len(w) <= 4 else rf"\b{re.escape(w)}"
            if re.search(pattern, blob):
                hits.add(theme)
                break
    return sorted(hits)
