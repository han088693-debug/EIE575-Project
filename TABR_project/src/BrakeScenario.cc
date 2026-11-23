#include "BrakeScenario.h"

#include <cmath>
#include <iomanip>

#include "veins/base/modules/BaseMobility.h"
#include "veins/modules/mobility/traci/TraCICommandInterface.h"
#include "veins/modules/mobility/traci/TraCIMobility.h"
#include "veins/modules/mobility/traci/TraCIScenarioManager.h"

using namespace omnetpp;

Define_Module(tabr::BrakeScenario);

namespace tabr {
namespace {
std::string csv(const std::string& value)
{
    std::string result = "\"";
    for (char c : value) result += c == '"' ? "\"\"" : std::string(1, c);
    return result + "\"";
}
}

BrakeScenario::~BrakeScenario()
{
    unsubscribe();
    cancelAndDelete(startBrake);
    cancelAndDelete(holdStop);
    cancelAndDelete(formalTick);
}

void BrakeScenario::initialize()
{
    manager = check_and_cast<veins::TraCIScenarioManager*>(
        getParentModule()->getSubmodule("manager"));
    outputDir = par("outputDir").stdstringValue();
    runId = par("runId").stdstringValue();
    leadId = par("leadId").stdstringValue();
    followId = par("followId").stdstringValue();
    brakeTime = par("brakeTime").doubleValue();
    brakeDuration = par("brakeDuration").doubleValue();
    targetSpeed = par("targetSpeed").doubleValue();
    updateInterval = par("updateInterval").doubleValue();
    deadline = par("deadline").doubleValue();
    observationWindow = par("observationWindow").doubleValue();
    if (par("formalMode").boolValue()) {
        formalKind = par("formalScenario").stdstringValue();
        const simtime_t warmup = SimTime(par("formalWarmup").doubleValue());
        formal.reset(new FormalScenario(manager, outputDir, runId, formalKind, warmup,
            SimTime(updateInterval), par("formalLocalRadius").doubleValue()));
        getParentModule()->subscribe(veins::BaseMobility::mobilityStateChangedSignal, this);
        subscribed = true;
        formalTick = new cMessage("formalPostMovementCensus");
        formalTick->setSchedulingPriority(2);
        scheduleAt(SimTime(updateInterval), formalTick);
        if (formalKind != "E0") {
            startBrake = new cMessage("formalScenarioCommand");
            startBrake->setSchedulingPriority(1);
            scheduleAt(formalKind == "S0" ? SimTime(updateInterval) : warmup, startBrake);
            if (formalKind != "S0") {
                holdStop = new cMessage("formalScenarioHold");
                holdStop->setSchedulingPriority(1);
            }
        }
        return;
    }
    if (leadId == followId || brakeTime <= 0 || brakeDuration <= 0
        || targetSpeed < 0 || updateInterval <= 0 || deadline <= 0
        || observationWindow < deadline) {
        throw cRuntimeError("BrakeScenario: invalid IDs, timing, or speed parameters");
    }

    truth.open((outputDir + "/scenario_truth.csv").c_str(), std::ios::trunc);
    if (!truth) throw cRuntimeError("Cannot write scenario_truth.csv; create outputDir first");
    truth << "run_id,t,vehicle_id,position_m,speed_mps,accel_mps2\n";
    truth << std::setprecision(17);
    writeEventSnapshot();

    getParentModule()->subscribe(veins::BaseMobility::mobilityStateChangedSignal, this);
    subscribed = true;
    startBrake = new cMessage("scenarioStartBrake");
    holdStop = new cMessage("scenarioHoldTargetSpeed");
    // Manager updates use the normal priority. Apply the command after that step.
    startBrake->setSchedulingPriority(1);
    holdStop->setSchedulingPriority(1);
    scheduleAt(SimTime(brakeTime), startBrake);
}

void BrakeScenario::handleMessage(cMessage* message)
{
    if (formal) {
        if (message == formalTick) {
            formal->census(simTime());
            scheduleAt(simTime()+SimTime(updateInterval),formalTick);
        } else if (message == startBrake || message == holdStop) {
            formal->command(message == holdStop);
            if (message == startBrake && holdStop)
                scheduleAt(simTime()+formal->brakeDuration(),holdStop);
        } else throw cRuntimeError("Unknown formal scenario message");
        return;
    }
    if (message != startBrake && message != holdStop) {
        throw cRuntimeError("BrakeScenario received an unexpected message");
    }
    const auto& hosts = manager->getManagedHosts();
    if (hosts.find(leadId) == hosts.end() || hosts.find(followId) == hosts.end()) {
        failScenario("target_vehicle_missing");
    }
    auto* traci = manager->getCommandInterface();
    if (!traci) failScenario("traci_not_ready");

    if (message == startBrake) {
        traci->vehicle(leadId).slowDown(targetSpeed, SimTime(brakeDuration));
        commandIssued = true;
        eventStatus = "command_issued";
        writeEventSnapshot();
        scheduleAt(simTime() + SimTime(brakeDuration), holdStop);
    }
    else {
        // Preserve the stopped leader after slowDown's transition has completed.
        traci->vehicle(leadId).setSpeed(targetSpeed);
    }
}

void BrakeScenario::receiveSignal(cComponent*, simsignal_t signal,
    cObject* value, cObject*)
{
    Enter_Method_Silent();
    if (signal != veins::BaseMobility::mobilityStateChangedSignal) return;
    auto* mobility = dynamic_cast<veins::TraCIMobility*>(value);
    if (!mobility) return;
    if (formal) {
        formal->observe(mobility, simTime());
        return;
    }
    const std::string id = mobility->getExternalId();
    if (id != leadId && id != followId) return;

    const double now = simTime().dbl();
    const double speed = mobility->getSpeed();
    const auto before = previous.find(id);
    bool hasAcceleration = before != previous.end();
    double acceleration = 0;
    if (hasAcceleration) {
        const double dt = now - before->second.time;
        if (dt <= 0) return;
        if (std::abs(dt - updateInterval) > 1e-7) {
            failScenario("mobility_update_interval_mismatch");
        }
        acceleration = (speed - before->second.speed) / dt;
        if (id == leadId && commandIssued && actualStart < 0
            && speed < before->second.speed - 1e-6) {
            actualStart = now;
            onsetLowerBound = before->second.time;
            eventStatus = "occurred";
            writeEventSnapshot();
        }
    }
    truth << csv(runId) << ',' << now << ',' << csv(id) << ','
          << mobility->getPositionAt(simTime()).x << ',' << speed << ',';
    if (hasAcceleration) truth << acceleration;
    truth << '\n';
    truth.flush();
    previous[id] = PreviousSample{now, speed};
}

void BrakeScenario::writeEventSnapshot()
{
    // Keep a planned row even if execution stops before a braking event exists.
    std::ofstream events((outputDir + "/event_log.csv").c_str(), std::ios::trunc);
    if (!events) throw cRuntimeError("Cannot write event_log.csv; create outputDir first");
    events << "run_id,planned_event_id,pair_id,sender_id,receiver_id,"
              "planned_time,t0,event_id,t_detect,deadline_s,deadline_at,obs_end,"
              "event_status,exclusion_reason,t0_lower_bound\n";
    events << std::setprecision(17)
           << csv(runId) << ',' << csv("lead-brake-1") << ','
           << csv(leadId + "->" + followId) << ',' << csv(leadId) << ','
           << csv(followId) << ',' << brakeTime << ',';
    if (actualStart >= 0) events << actualStart;
    // event_id and t_detect belong to the application; the analyzer fills them.
    events << ",,," << deadline << ',';
    if (actualStart >= 0) events << actualStart + deadline;
    events << ',';
    if (actualStart >= 0) events << actualStart + observationWindow;
    events << ',' << csv(eventStatus) << ',' << csv(errorReason) << ',';
    if (onsetLowerBound >= 0) events << onsetLowerBound;
    events << '\n';
}

void BrakeScenario::failScenario(const std::string& reason)
{
    eventStatus = "scenario_error";
    errorReason = reason;
    writeEventSnapshot();
    throw cRuntimeError("BrakeScenario: %s", reason.c_str());
}

void BrakeScenario::unsubscribe()
{
    if (subscribed && getParentModule()) {
        getParentModule()->unsubscribe(veins::BaseMobility::mobilityStateChangedSignal, this);
        subscribed = false;
    }
}

void BrakeScenario::finish()
{
    if (formal) {
        formal->finish(simTime());
        formal.reset();
        unsubscribe();
        return;
    }
    if (actualStart < 0 && eventStatus != "scenario_error") {
        eventStatus = "scenario_error";
        errorReason = commandIssued ? "no_observed_braking" : "brake_command_not_reached";
    }
    if (actualStart >= 0 && simTime().dbl() + 1e-7 < actualStart + observationWindow) {
        eventStatus = "scenario_error";
        errorReason = "incomplete_observation_window";
    }
    writeEventSnapshot();
    truth.flush();
    unsubscribe();
}

} // namespace tabr
