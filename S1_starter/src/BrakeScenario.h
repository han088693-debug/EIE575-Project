#pragma once

#include <omnetpp.h>
#include <fstream>
#include <map>
#include <string>

namespace veins {
class TraCIScenarioManager;
}

namespace tabr {

class BrakeScenario : public omnetpp::cSimpleModule, public omnetpp::cListener {
public:
    ~BrakeScenario() override;

protected:
    void initialize() override;
    void handleMessage(omnetpp::cMessage* message) override;
    void receiveSignal(omnetpp::cComponent* source, omnetpp::simsignal_t signal,
        omnetpp::cObject* value, omnetpp::cObject* details) override;
    void finish() override;

private:
    struct PreviousSample {
        double time;
        double speed;
    };

    veins::TraCIScenarioManager* manager = nullptr;
    omnetpp::cMessage* startBrake = nullptr;
    omnetpp::cMessage* holdStop = nullptr;
    std::ofstream truth;
    std::map<std::string, PreviousSample> previous;
    std::string outputDir;
    std::string runId;
    std::string leadId;
    std::string followId;
    std::string eventStatus = "planned";
    std::string errorReason;
    double brakeTime = 0;
    double brakeDuration = 0;
    double targetSpeed = 0;
    double updateInterval = 0;
    double deadline = 0;
    double observationWindow = 0;
    double actualStart = -1;
    double onsetLowerBound = -1;
    bool commandIssued = false;
    bool subscribed = false;

    void writeEventSnapshot();
    void failScenario(const std::string& reason);
    void unsubscribe();
};

} // namespace tabr
