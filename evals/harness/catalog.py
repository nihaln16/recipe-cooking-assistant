from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EVALS_ROOT = Path(__file__).resolve().parents[1]
CASES_DIR = EVALS_ROOT / "cases"
RESULTS_DIR = EVALS_ROOT / "results"
LOCAL_DIR = EVALS_ROOT / "local"


@dataclass(frozen=True)
class EvalCase:
    id: str
    path: Path
    data: dict[str, Any]

    @property
    def source_text(self) -> str | None:
        inline = self.data.get("source_text")
        if inline:
            return inline
        rel = self.data.get("source_file")
        if rel:
            return (self.path.parent / rel).read_text(encoding="utf-8")
        return None

    @property
    def mock_output(self) -> dict[str, Any]:
        inline = self.data.get("mock_output")
        if inline is not None:
            return inline
        rel = self.data.get("mock_output_file", "mock_output.json")
        return json.loads((self.path.parent / rel).read_text(encoding="utf-8"))

    @property
    def expect(self) -> dict[str, Any]:
        return self.data.get("expect", {})

    @property
    def live(self) -> dict[str, Any]:
        return self.data.get("live", {})

    @property
    def live_enabled(self) -> bool:
        return bool(self.live.get("enabled", False))

    @property
    def estimated_max_cost_usd(self) -> float:
        return float(self.live.get("estimated_max_cost_usd", 0.08))

    def live_image_paths(self) -> list[Path]:
        paths: list[Path] = []
        for rel in self.live.get("image_paths", []):
            path = Path(rel)
            if not path.is_absolute():
                path = EVALS_ROOT / path
            paths.append(path)
        return paths


def load_cases(*, ids: list[str] | None = None) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for case_file in sorted(CASES_DIR.glob("*/case.json")):
        data = json.loads(case_file.read_text(encoding="utf-8"))
        case = EvalCase(id=data["id"], path=case_file, data=data)
        if ids is not None and case.id not in ids:
            continue
        cases.append(case)
    return cases


def list_case_ids() -> list[str]:
    return [c.id for c in load_cases()]
