"""Verify application timers at a vehicle's last SUMO / Veins manager step."""
from decimal import Decimal
import xml.etree.ElementTree as ET


def completed_trips(root, diagnostic):
    relative = diagnostic.get("raw_evidence_paths", {}).get("trips.xml")
    if not relative:
        return {}
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("SUMO trip report outside attempt")
    arrivals = {}
    try:
        for _, element in ET.iterparse(path, events=("end",)):
            if element.tag == "tripinfo":
                row = element.attrib
                arrival = Decimal(row["arrival"])
                if not arrival.is_finite():
                    raise ValueError("Nonfinite SUMO arrival")
                if arrival >= 0 and row.get("vaporized", "").lower() in ("", "0", "false"):
                    if row["id"] in arrivals:
                        raise ValueError("Duplicate SUMO normal arrival")
                    arrivals[row["id"]] = arrival
            element.clear()
    except ET.ParseError as error:
        raise ValueError(f"Malformed SUMO trip report: {error}") from error
    return arrivals


def check_final_control(vehicle, end, last, control_step, motion_step, eps, arrivals):
    remainder = end - last
    if remainder < control_step - eps:
        return False
    arrival = arrivals.get(vehicle)
    # The manager removes an arrived SUMO vehicle on its next motion update.
    # Removal cancels the same-time application's scheduled terminal control tick.
    if (arrival is None or abs(arrival+motion_step-end) > eps or
            abs(remainder-control_step) > eps):
        raise ValueError(f"Truncated terminal control trace for {vehicle}")
    return True
