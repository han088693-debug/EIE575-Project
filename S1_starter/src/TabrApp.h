#pragma once

#include <fstream>
#include <map>
#include <memory>
#include <string>
#include "veins/modules/application/ieee80211p/DemoBaseApplLayer.h"
#include "veins/modules/mac/ieee80211p/Mac1609_4.h"
#include "TabrCore.h"
#include "TabrBeacon_m.h"

namespace tabr {

class TabrApp : public veins::DemoBaseApplLayer {
public:
    ~TabrApp() override;
    void initialize(int stage) override;
    void finish() override;
    using DemoBaseApplLayer::receiveSignal;
    void receiveSignal(omnetpp::cComponent*, omnetpp::simsignal_t, bool, omnetpp::cObject*) override;

protected:
    void handleSelfMsg(omnetpp::cMessage*) override;
    void handlePositionUpdate(omnetpp::cObject*) override;
    void onBSM(veins::DemoSafetyMessage*) override;

private:
    struct State {
        bool known = false;
        long seq = -1;
        double generated = -1;
        double x = 0;
        double speed = 0;
    };

    void sampleMotion();
    void control();
    void transmit();
    void rescheduleSend();
    void writeStates();
    void openLog(std::ofstream&, const std::string&, const std::string&);

    bool ready = false;
    bool smoothed = false;
    bool brake = false;
    bool hasSubmitted = false;
    std::string vehicleId, scheme, outputDir, runId;
    double period = 0.1, lookback = 0.5, validity = 2;
    double threshold = 4, errorScale = 0.5, speedReference = 9.166666666666667;
    double rMin = 1, rMax = 10, beta = 2, base = 2;
    double target = 0.6, deadband = 0.03, eta = 0.5, smoothOld = 0.8, jitterFraction = 0.05;
    double C = 0, Cbar = 0, innovation = 0, S = 0, U = 0, W = 1, rate = 2;
    double x = 0, speed = 0, accel = 0;
    double windowStart = 0, lastMotion = -1, lastSubmit = 0, jitter = 1;
    double eventDetected = -1, eventUntil = -1;
    long sequence = 0, eventId = -1;
    int messageBits = 2000;
    std::unique_ptr<MotionHistory> history;
    std::unique_ptr<BusyWindow> busyWindow;
    veins::Mac1609_4* busySource = nullptr;
    int busySourceId = -1;
    omnetpp::cMessage* controlTimer = nullptr;
    omnetpp::cMessage* sendTimer = nullptr;
    std::map<std::string, State> states;
    std::ofstream controlLog, txLog, rxLog, truthLog, stateLog, busyLog, detectionLog;
};

} // namespace tabr
