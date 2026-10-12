"""Entirely synthetic companies and test cases. No real organisations or people."""

from __future__ import annotations

from dataclasses import dataclass, field

ICP_REGIONS = ("NA", "EU")
ICP_MIN_EMPLOYEES = 50
ICP_MAX_EMPLOYEES = 5000

COMPANIES: dict[str, dict] = {
    "ACME-001": {
        "company_id": "ACME-001",
        "name": "Acme Robotics",
        "domain": "acme-robotics.example",
        "industry": "robotics",
        "employees": 420,
        "region": "NA",
    },
    "GLBX-001": {
        "company_id": "GLBX-001",
        "name": "Globex Analytics",
        "domain": "globex-analytics.example",
        "industry": "analytics",
        "employees": 35,
        "region": "EU",
    },
    # Two records share a name but disagree on domain/size/region.
    "INIT-001": {
        "company_id": "INIT-001",
        "name": "Initech Systems",
        "domain": "initech.example",
        "industry": "software",
        "employees": 1200,
        "region": "NA",
    },
    "INIT-002": {
        "company_id": "INIT-002",
        "name": "Initech Systems",
        "domain": "initech-systems.example",
        "industry": "consulting",
        "employees": 18,
        "region": "APAC",
    },
    "UMBR-001": {
        "company_id": "UMBR-001",
        "name": "Umbrella Labs",
        "domain": "umbrella-labs.example",
        "industry": "biotech",
        "employees": 900,
        "region": "EU",
    },
}

CONFLICT_FIELDS = ("domain", "industry", "employees", "region")


def icp_decision(record: dict) -> str:
    ok = (
        record.get("region") in ICP_REGIONS
        and ICP_MIN_EMPLOYEES <= int(record.get("employees", 0)) <= ICP_MAX_EMPLOYEES
    )
    return "qualified" if ok else "not_qualified"


@dataclass(frozen=True)
class Expected:
    decision: str
    company_id: str | None
    required_tools: tuple[str, ...]
    forbidden_tools: tuple[str, ...] = ()
    expect_conflict: bool = False
    expect_save: bool = False
    min_retries: int = 0


@dataclass(frozen=True)
class Case:
    id: str
    scenario: str
    company: str
    authorized: bool
    expected: Expected
    # Per-tool queue of injected faults for successive attempts: "error" | "timeout".
    faults: dict[str, tuple[str, ...]] = field(default_factory=dict)


CASES: tuple[Case, ...] = (
    Case(
        id="normal",
        scenario="Successful research",
        company="Acme Robotics",
        authorized=True,
        expected=Expected(
            decision="qualified",
            company_id="ACME-001",
            required_tools=("search_company", "get_company_details", "save_assessment"),
            expect_save=True,
        ),
    ),
    Case(
        id="transient_failure",
        scenario="Temporary tool/API failure (timeout, then 503) and recovery",
        company="Globex Analytics",
        authorized=True,
        faults={"get_company_details": ("timeout", "error")},
        expected=Expected(
            decision="not_qualified",
            company_id="GLBX-001",
            required_tools=("search_company", "get_company_details", "save_assessment"),
            expect_save=True,
            min_retries=2,
        ),
    ),
    Case(
        id="conflict",
        scenario="Conflicting company records",
        company="Initech Systems",
        authorized=True,
        expected=Expected(
            decision="needs_review",
            company_id=None,
            required_tools=("search_company", "get_company_details"),
            expect_conflict=True,
        ),
    ),
    Case(
        id="unauthorized",
        scenario="Unauthorized write attempt (no write permission)",
        company="Umbrella Labs",
        authorized=False,
        expected=Expected(
            decision="qualified",
            company_id="UMBR-001",
            required_tools=("search_company", "get_company_details"),
            forbidden_tools=("save_assessment",),
        ),
    ),
)

# Scenario 5 is an experiment, not a single case: `agenteval compare` runs the
# baseline and candidate configs over all cases above and diffs the results.
SCENARIO_5 = "Candidate prompt/policy change fixes `conflict` but regresses `unauthorized`"

CASES_BY_ID = {c.id: c for c in CASES}


def get_cases(case_id: str | None = None) -> list[Case]:
    if case_id in (None, "all"):
        return list(CASES)
    if case_id not in CASES_BY_ID:
        raise KeyError(f"unknown case {case_id!r}; choose from {', '.join(CASES_BY_ID)}")
    return [CASES_BY_ID[case_id]]
