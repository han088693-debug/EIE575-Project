"""Explicit local paths; no process-global changes to the simulation installation."""
from dataclasses import dataclass
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    source_project: Path
    tools_root: Path
    state_root: Path
    max_run_wall_s: int = 1800

    @classmethod
    def load(cls):
        path = Path(os.environ.get("EIE575_MCP_SETTINGS", HERE / "settings.json"))
        values = json.loads(path.read_text(encoding="utf-8-sig"))
        paths = {key: Path(values[key]).resolve() for key in
                 ("source_project", "tools_root", "state_root")}
        for key in ("tools_root", "state_root"):
            if not str(paths[key]).isascii() or " " in str(paths[key]):
                raise ValueError(f"{key} must use an ASCII path without spaces")
        if paths["state_root"].is_relative_to(paths["source_project"]):
            raise ValueError("state_root must be outside source_project")
        limit = values.get("max_run_wall_s", 1800)
        if type(limit) is not int or not 30 <= limit <= 86400:
            raise ValueError("max_run_wall_s must be an integer between 30 and 86400")
        return cls(**paths, max_run_wall_s=limit)

    def as_dict(self):
        return {"source_project": str(self.source_project), "tools_root": str(self.tools_root),
                "state_root": str(self.state_root), "max_run_wall_s": self.max_run_wall_s}

    def check(self):
        required = [self.source_project / name for name in
                    ("EIE575_TABR_Starter.exe", "omnetpp.ini", "radio.xml", "tools/manage.py",
                     "tools/analyze.py", "scenarios/s1.launchd.xml", "src/ResearchHighway.ned")]
        required += [self.tools_root / name for name in
                     ("sumo-1.6.0/bin/sumo.exe", "sumo-1.6.0/bin/netconvert.exe",
                      "omnetpp-5.6.2/tools/win64/mingw64/bin/python2.exe",
                      "veins-5.0/sumo-launchd.py", "veins-5.0/src/libveins.dll")]
        missing = [str(path) for path in required if not path.is_file()]
        return {"ready": os.name == "nt" and not missing, "missing": missing,
                "platform": os.name, "settings": self.as_dict(),
                "scope": "S1 two-vehicle 30-second functional test; not S0/S2 formal experiments"}
