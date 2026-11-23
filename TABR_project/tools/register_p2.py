"""Pre-register the P2 probes from the core candidate, before running them."""
import argparse
import copy
import json
from pathlib import Path
from experiment_config import load_config

PROJECT = Path(__file__).resolve().parents[1]


def write(path, value):
    content = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8-sig") == content:
        return
    new = path.with_suffix(".tmp")
    new.write_text(content, encoding="utf-8")
    new.replace(path)


def register():
    root = PROJECT / "configs"
    base = load_config(root / "core.json")
    if base["stage"] != "candidate":
        raise ValueError("P2 probes require the candidate protocol")
    entries = {}
    seed = base["seeds"]["acceptance"]

    def add(identifier, config, scenario, scheme, density, name):
        dest = root / "p2" / (name + ".json")
        write(dest, config)
        entries[identifier] = dict(scope="engineering", config=dest.relative_to(root).as_posix(),
            scenario=scenario, load_id="low" if density <= 5 else "high", scheme=scheme,
            seed=seed, density_per_lane_km=density)

    for count, density in [(16, 0.5), (64, 2), (160, 5), (480, 15), (960, 30)]:
        cfg = copy.deepcopy(base)
        cfg["observation_mode"] = "registered_pairs"
        cfg["resource_probe"] = dict(background_vehicles=count, logging_mode="registered_pairs", duration_s=4)
        for scheme in ("G0", "G1"):
            add(f"p2_resource_{count}_{scheme}", cfg, "E0", scheme, density, f"resource_{count}")
    for mode in ("full", "registered_pairs"):
        cfg = copy.deepcopy(base)
        cfg["observation_mode"] = mode
        cfg["engineering_geometry"] = {"background_vehicles":16}
        for scenario in ("S1","S0","S2"):
            add(f"p2_{scenario}_{mode}", cfg, scenario, "G3", 0.5, f"function_{mode}")
            if mode == "registered_pairs":
                for scheme in ("G0", "G1", "G2"):
                    add(f"p2_{scenario}_{scheme}", cfg, scenario, scheme, 0.5, f"function_{mode}")
    loss = copy.deepcopy(base)
    loss["observation_mode"] = "registered_pairs"
    loss["engineering_geometry"] = {"background_vehicles":16}
    loss["engineering_purpose"] = "Real radio no-reception acceptance control; exclude from formal jobs and E0"
    loss["wireless"]["noise_floor_dbm"] = 0
    add("p2_S1_radio_loss", loss, "S1", "G3", 0.5, "receiver_loss")
    reg = root / "formal_probes.json"
    existing = json.loads(reg.read_text(encoding="utf-8-sig"))["probes"] if reg.exists() else {}
    existing.update(entries)
    write(reg, {"probes":existing})
    return sorted(entries)


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(json.dumps(register()))
