from __future__ import print_function

import argparse
import datetime
import json
import os
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def mkdir(path):
    if not os.path.isdir(path):
        os.makedirs(path)


def unix_path(path):
    path = os.path.abspath(path).replace("\\", "/")
    return "/" + path[0].lower() + path[2:] if path[1:2] == ":" else path


def quote(text):
    return "'" + text.replace("'", "'\\''") + "'"


def tool_paths(base):
    omnet = os.path.join(base, "omnetpp-5.6.2")
    veins = os.path.join(base, "veins-5.0")
    sumo = os.path.join(base, "sumo-1.6.0", "bin")
    mingw = os.path.join(omnet, "tools", "win64", "mingw64", "bin")
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([os.path.join(omnet, "bin"), mingw,
                                  os.path.join(veins, "src"), sumo, env.get("PATH", "")])
    env["QT_PLUGIN_PATH"] = os.path.join(omnet, "tools", "win64", "mingw64", "share", "qt5", "plugins")
    env["MSYSTEM"] = "MINGW64"
    return omnet, veins, sumo, mingw, env


def build(base):
    omnet, veins, sumo, mingw, env = tool_paths(base)
    bash = os.path.join(omnet, "tools", "win64", "usr", "bin", "bash.exe")
    commands = [
        "export OMNETPP_ROOT=" + quote(unix_path(omnet)),
        'export PATH="$OMNETPP_ROOT/bin:/mingw64/bin:/usr/bin:/bin:/c/Windows/System32:/c/Windows"',
        "export TCL_LIBRARY=/mingw64/lib/tcl8.6",
        "export QT_PLUGIN_PATH=/mingw64/share/qt5/plugins",
        "make -C " + quote(unix_path(os.path.join(veins, "src"))) +
        " MODE=release -W Makefile " + quote("SHLIB_LD=$(CXX) -shared -Wl,--export-all-symbols"),
        "cd " + quote(unix_path(ROOT)),
        "opp_makemake -f --deep -Xtests -O out -o EIE575_TABR_Starter -DVEINS_IMPORT -Isrc " +
        "-I" + quote(unix_path(os.path.join(veins, "src"))) + " " +
        "-L" + quote(unix_path(os.path.join(veins, "src"))) + " -lveins",
        "make MODE=release -j4",
    ]
    process = subprocess.Popen([bash, "--noprofile", "--norc", "-c", " && ".join(commands)],
                               cwd=ROOT, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
    output = process.communicate()[0]
    with open(os.path.join(ROOT, "build.log"), "wb") as handle:
        handle.write(output)
    print(output.decode("utf-8", "replace"))
    if process.returncode:
        raise RuntimeError("Build failed; see build.log")


def prepare(base):
    omnet, veins, sumo, mingw, env = tool_paths(base)
    scenario = os.path.join(ROOT, "scenarios")
    subprocess.check_call([os.path.join(sumo, "netconvert.exe"),
                           "--node-files", "highway.nod.xml",
                           "--edge-files", "highway.edg.xml",
                           "--output-file", "highway.net.xml"],
                          cwd=scenario, env=env, creationflags=NO_WINDOW)
    tree = ET.parse(os.path.join(scenario, "s1.launchd.xml"))
    tree.getroot().find("basedir").set("path", scenario.replace("\\", "/"))
    mkdir(os.path.join(ROOT, "generated"))
    tree.write(os.path.join(ROOT, "generated", "s1.launchd.xml"), encoding="utf-8",
               xml_declaration=True)


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def run(base, scheme, seed, gui):
    prepare(base)
    omnet, veins, sumo, mingw, env = tool_paths(base)
    exe = os.path.join(ROOT, "EIE575_TABR_Starter.exe")
    if not os.path.isfile(exe):
        raise RuntimeError("Build first: .\\run.ps1 -Action build")
    run_id = "%s_seed%d_%s" % (scheme, seed, datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    result = os.path.join(ROOT, "results", run_id)
    os.makedirs(result)
    port = free_port()
    command = [
        exe, "-u", "Qtenv" if gui else "Cmdenv", "-f", "omnetpp.ini", "-c", scheme,
        "-n", os.path.join(ROOT, "src") + ";" + os.path.join(veins, "src", "veins"),
        "--seed-set=" + str(seed), "--*.manager.seed=" + str(seed),
        "--*.manager.port=" + str(port), "--result-dir=" + result.replace("\\", "/"),
        '--*.node[*].appl.runId="' + run_id + '"',
        '--*.brakeScenario.runId="' + run_id + '"',
    ]
    metadata = {"run_id": run_id, "scheme": scheme, "seed": seed,
                "scope": "S1 two-vehicle functional smoke test", "command": command}
    with open(os.path.join(result, "run.json"), "w") as handle:
        json.dump(metadata, handle, indent=2)
    launcher_output = open(os.path.join(result, "launcher_console.log"), "wb")
    simulation_output = open(os.path.join(result, "simulation.log"), "wb")
    launcher = subprocess.Popen(
        [os.path.join(mingw, "python2.exe"), "-u", os.path.join(veins, "sumo-launchd.py"),
         "-c", os.path.join(sumo, "sumo.exe"), "-b", "127.0.0.1", "-p", str(port),
         "-L", os.path.join(result, "launchd.log"), "-v"],
        cwd=ROOT, env=env, stdout=launcher_output, stderr=subprocess.STDOUT,
        creationflags=NO_WINDOW)
    try:
        deadline = time.time() + 15
        while True:
            if launcher.poll() is not None:
                raise RuntimeError("SUMO launcher exited; see " + result)
            probe = socket.socket()
            probe.settimeout(0.2)
            ready = probe.connect_ex(("127.0.0.1", port)) == 0
            probe.close()
            if ready:
                break
            if time.time() >= deadline:
                raise RuntimeError("SUMO launcher did not become ready; see " + result)
            time.sleep(0.1)
        print("Running", run_id)
        code = subprocess.call(command, cwd=ROOT, env=env, stdout=simulation_output,
                               stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        if code:
            raise RuntimeError("Simulation failed (exit %d); see %s" % (code, result))
    finally:
        if launcher.poll() is None:
            launcher.terminate()
        launcher.wait()
        launcher_output.close()
        simulation_output.close()
    subprocess.check_call([sys.executable, os.path.join(ROOT, "tools", "analyze.py"), result],
                          creationflags=NO_WINDOW)
    print("Results:", result)
    return result


def main():
    parser = argparse.ArgumentParser(description="Build and run the EIE575 TABR starter.")
    parser.add_argument("action", choices=["build", "prepare", "run"])
    parser.add_argument("--tools", default="E:/EIE575_Tools")
    parser.add_argument("--scheme", choices=["G0", "G1", "G2", "G3"], default="G0")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--gui", action="store_true")
    args = parser.parse_args()
    try:
        ROOT.encode("ascii")
        args.tools.encode("ascii")
    except UnicodeError:
        raise RuntimeError("Use an ASCII working path, e.g. E:/EIE575_Tools/workspace/EIE575_TABR_Starter")
    if args.action == "build":
        build(args.tools)
    elif args.action == "prepare":
        prepare(args.tools)
    else:
        run(args.tools, args.scheme, args.seed, args.gui)


if __name__ == "__main__":
    main()
