#pragma once

#include <omnetpp.h>
#include <fstream>
#include <map>
#include <set>
#include <string>
#include <vector>

namespace veins {
class TraCIMobility;
class TraCIScenarioManager;
}

namespace tabr {

class FormalScenario {
public:
    FormalScenario(veins::TraCIScenarioManager* manager, const std::string& directory,
                   const std::string& run, const std::string& scenario, omnetpp::simtime_t warmup,
                   omnetpp::simtime_t step, double radius);
    ~FormalScenario();
    void observe(veins::TraCIMobility* mobility, omnetpp::simtime_t now);
    void command(bool hold);
    void census(omnetpp::simtime_t now);
    void finish(omnetpp::simtime_t now);
    omnetpp::simtime_t brakeDuration() const { return duration; }

private:
    struct Event {
        std::string id;
        double targetSpeed = 0;
        omnetpp::simtime_t lower = -1, onset = -1;
        std::string status = "planned", reason;
    };
    struct Sample { omnetpp::simtime_t time; double speed; };
    veins::TraCIScenarioManager* manager;
    std::string directory, run, scenario;
    omnetpp::simtime_t warmup, step, duration = 0, window = 0;
    double radius;
    bool issued = false;
    int streams = 0;
    std::ofstream truth, supply;
    std::set<std::string> tracked, targets, background, coordinateChecked;
    std::map<std::string,Event> events;
    std::map<std::string,Sample> samples;
    void censusCoordinates();
    void writeEvents();
    void changeStreams(int difference);
};

}
