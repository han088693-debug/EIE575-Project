#pragma once
#include <cstdint>
#include <fstream>
#include <map>
#include <set>
#include <string>
#include <vector>
#include <omnetpp.h>

namespace tabr {

struct Prior {
    omnetpp::simtime_t time;
    double x, speed;
};

class FormalObservation {
public:
    static FormalObservation& open(const std::string& folder, const std::string& run);
    static void scenarioStreams(int number);
    std::ofstream control, tx, rx, busy, detection, state, lifecycle;
    const std::vector<std::string>& observedBy(const std::string& receiver) const;
    const Prior* priorFor(const std::string& sender, const std::string& receiver) const;
    bool tracksSender(const std::string& sender) const { return senders.count(sender); }
    bool auditsBusy(const std::string& vehicle) const { return audited.count(vehicle); }
    double phase(const std::string& id) const;
    void vehicleEntered();
    void vehicleLeft();
    static double jitterFor(const std::string& vehicle, int seed, long sendIndex, double fraction);
private:
    FormalObservation(const std::string& folder, const std::string& run);
    void counters();
    std::ofstream counterLog;
    int active = 0, peak = 0;
    std::string runId;
    std::map<std::string,std::vector<std::string>> receivers;
    std::map<std::pair<std::string,std::string>,Prior> priors;
    std::map<std::string,double> phases;
    std::set<std::string> senders, audited;
};

}
