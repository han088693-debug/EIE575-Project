#include "TabrApp.h"
#include <iomanip>

using namespace omnetpp;
using namespace veins;

Define_Module(tabr::TabrApp);

namespace tabr {

TabrApp::~TabrApp()
{
    cModule* source = busySourceId < 0 ? nullptr : getSimulation()->getModule(busySourceId);
    if (source) source->unsubscribe(Mac1609_4::sigChannelBusy, this);
    cancelAndDelete(controlTimer);
    cancelAndDelete(sendTimer);
    cancelAndDelete(stateTimer);
}

void TabrApp::openLog(std::ofstream& stream, const std::string& name, const std::string& header)
{
    const std::string filename = outputDir + "/" + name + "_" + vehicleId + ".csv";
    stream.open(filename.c_str());
    if (!stream) throw cRuntimeError("Cannot write %s; create a fresh output directory first", filename.c_str());
    stream << std::setprecision(15) << header << '\n';
}

void TabrApp::initialize(int stage)
{
    DemoBaseApplLayer::initialize(stage);
    if (stage != 1) return;
    if (!mobility || sendBeacons || mac->isChannelSwitchingActive())
        throw cRuntimeError("TABR requires TraCI mobility, sendBeacons=false and one channel");
    scheme = par("scheme").stdstringValue();
    outputDir = par("outputDir").stdstringValue();
    runId = par("runId").stdstringValue();
    vehicleId = mobility->getExternalId();
    if (vehicleId.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-") != std::string::npos)
        throw cRuntimeError("Vehicle IDs must use ASCII letters, digits, underscore, dot or hyphen");
    period = par("controlPeriod").doubleValue();
    lookback = par("lookback").doubleValue();
    validity = par("eventValidity").doubleValue();
    threshold = par("brakeThreshold").doubleValue();
    errorScale = par("errorScale").doubleValue();
    speedReference = par("speedReference").doubleValue();
    rMin = par("rMinHz").doubleValue();
    rMax = par("rMaxHz").doubleValue();
    base = par("initialBaseHz").doubleValue();
    beta = par("beta").doubleValue();
    target = par("cbrTarget").doubleValue();
    deadband = par("cbrDeadband").doubleValue();
    eta = par("feedbackGain").doubleValue();
    smoothOld = par("smoothingOld").doubleValue();
    jitterFraction = par("jitterFraction").doubleValue();
    messageBits = par("messageBits").intValue();
    if (period <= 0 || lookback <= 0 || validity <= 0 || threshold <= 0 ||
        errorScale <= 0 || speedReference <= 0 || rMin <= 0 || rMax < rMin ||
        beta < 0 || base < rMin / (1 + beta) || base > rMax ||
        target < 0 || target > 1 || deadband < 0 || eta < 0 ||
        smoothOld < 0 || smoothOld >= 1 || jitterFraction < 0 || jitterFraction >= 1 || messageBits <= 0)
        throw cRuntimeError("Invalid TABR control parameter");
    weight(scheme, false, 0, 0, speedReference, beta);
    history.reset(new MotionHistory(lookback));
    busyWindow.reset(new BusyWindow(simTime().dbl()));
    busySource = FindModule<Mac1609_4*>::findSubModule(getParentModule());
    if (!busySource) throw cRuntimeError("Mac1609_4 not found");
    busySourceId = busySource->getId();
    busySource->subscribe(Mac1609_4::sigChannelBusy, this);
    if (par("formalMode").boolValue()) {
        formal = &FormalObservation::open(outputDir,runId);
        fullLog = par("formalFullLog").boolValue();
        formalSeed = par("formalSeed").intValue();
        warmup = par("formalWarmup");
        stateStep = par("formalGrid");
        if (stateStep <= SIMTIME_ZERO) throw cRuntimeError("formalGrid must be positive");
        enterTime = simTime();
        observedSenders = formal->observedBy(vehicleId);
        observedSet.insert(observedSenders.begin(),observedSenders.end());
        for (const auto& sender : observedSenders) states.emplace(sender,State());
        logBusy = fullLog || formal->auditsBusy(vehicleId);
        logTx = fullLog || formal->tracksSender(vehicleId);
        // Each formal route is a single straight, eastbound edge. Calibrate the
        // front-bumper lane coordinate to the antenna position once, at insertion.
        laneOffset = mobility->getVehicleCommandInterface()->getLanePosition() - mobility->getPositionAt(simTime()).x;
        formal->vehicleEntered();
        if (logBusy) formal->busy << runId << ',' << vehicleId << ',' << simTime().str() << ",0\n";
    } else {
    openLog(controlLog, "control_log",
        "run_id,vehicle_id,tick_time,window_start,window_end,busy_duration_s,C,Cbar,e_H,B,S,U,b,r,speed_mps,W");
    openLog(txLog, "tx_log",
        "run_id,sender_id,seq,t_gen,t_submit,position_m,speed_mps,brake_active,event_id,event_detect_time,event_valid_until,message_bits");
    openLog(rxLog, "rx_log",
        "run_id,receiver_id,sender_id,seq,t_rx,t_gen,event_id,event_detect_time,event_valid_until,position_m,speed_mps,brake_active,state_valid,event_valid,invalid_reason");
    openLog(truthLog, "truth_log", "run_id,t,vehicle_id,position_m,speed_mps,accel_mps2");
    openLog(stateLog, "state_log",
        "run_id,t,receiver_id,sender_id,known,state_source,last_seq,last_state_gen_time,p_last_m,v_last_mps,p_hat_m,Age_s");
    openLog(busyLog, "busy_audit", "run_id,vehicle_id,transition_time,busy_state");
    openLog(detectionLog, "detection_log",
        "run_id,sender_id,event_id,t_detect,event_valid_until");
    busyLog << runId << ',' << vehicleId << ',' << simTime().dbl() << ",0\n";
    const std::string targetId = par("targetId").stdstringValue();
    if (vehicleId != targetId) states[targetId] = State();
    }
    windowStart = simTime().dbl();
    ready = true;
    sampleMotion();
    W = weight(scheme, brake, S, speed, speedReference, beta);
    U = beta > 0 ? (W - 1) / beta : 0;
    rate = clip(base * W, rMin, rMax);
    controlTimer = new cMessage("TABR control");
    sendTimer = new cMessage("TABR send");
    controlTimer->setSchedulingPriority(5);
    sendTimer->setSchedulingPriority(6);
    scheduleAt(simTime() + period, controlTimer);
    const double phase = formal ? formal->phase(vehicleId) : uniform(1.0 / rMax, 1.0 / rMin);
    if (formal && (!std::isfinite(phase) || phase < 1/rMax-1e-12 || phase > 1/rMin+1e-12))
        throw cRuntimeError("formal phase out of bounds: %s",vehicleId.c_str());
    scheduleAt(simTime() + phase, sendTimer);
    if (formal && !observedSenders.empty()) {
        stateTimer = new cMessage("TABR global-grid observation");
        stateTimer->setSchedulingPriority(32000);
        const int64_t n = (simTime().raw()+stateStep.raw()-1) / stateStep.raw();
        scheduleAt(SimTime::fromRaw(n*stateStep.raw()),stateTimer);
    }
}

double TabrApp::longitudinalPosition() const
{
    return mobility->getPositionAt(simTime()).x + laneOffset;
}

void TabrApp::sampleMotion()
{
    const double now = simTime().dbl();
    if (now == lastMotion) return;
    x = longitudinalPosition();
    speed = mobility->getSpeed();
    accel = history->push(now, x, speed);
    innovation = history->innovation();
    S = clip(innovation / errorScale, 0, 1);
    const bool nextBrake = accel <= -threshold;
    if (nextBrake && !brake && now >= eventUntil) {
        ++eventId;
        eventDetected = now;
        eventUntil = now + validity;
        if (formal) formal->detection << runId << ',' << vehicleId << ',' << eventId << ','
                                      << simTime().str() << ',' << SimTime(eventUntil).str() << '\n';
        else detectionLog << runId << ',' << vehicleId << ',' << eventId << ',' << eventDetected << ',' << eventUntil << '\n';
    }
    brake = nextBrake;
    lastMotion = now;
    if (!formal) truthLog << runId << ',' << now << ',' << vehicleId << ',' << x << ',' << speed << ',' << accel << '\n';
}

void TabrApp::handlePositionUpdate(cObject* object)
{
    DemoBaseApplLayer::handlePositionUpdate(object);
    if (ready) sampleMotion();
}

void TabrApp::receiveSignal(cComponent*, simsignal_t signal, bool busy, cObject*)
{
    Enter_Method_Silent();
    if (!ready || signal != Mac1609_4::sigChannelBusy) return;
    busyWindow->change(simTime().dbl(), busy);
    if (formal && logBusy) formal->busy << runId << ',' << vehicleId << ',' << simTime().str() << ',' << busy << '\n';
    if (!formal) busyLog << runId << ',' << vehicleId << ',' << simTime().dbl() << ',' << busy << '\n';
}

void TabrApp::handleSelfMsg(cMessage* message)
{
    if (message == controlTimer) {
        control();
        scheduleAt(simTime() + period, controlTimer);
    }
    else if (message == sendTimer) transmit();
    else if (message == stateTimer) {
        writeObservedStates();
        scheduleAt(simTime()+stateStep,stateTimer);
    }
    else DemoBaseApplLayer::handleSelfMsg(message);
}

void TabrApp::control()
{
    const double now = simTime().dbl();
    const double busyDuration = busyWindow->take(now);
    C = clip(busyDuration / (now - windowStart), 0, 1);
    Cbar = smoothed ? smoothOld * Cbar + (1 - smoothOld) * C : C;
    smoothed = true;
    if (controlActive(now, kEstimateSettleSeconds, smoothed)) {
        base = updateBase(base, Cbar, target, deadband, eta, rMin / (1 + beta), rMax);
    }
    W = weight(scheme, brake, S, speed, speedReference, beta);
    U = beta > 0 ? (W - 1) / beta : 0;
    rate = clip(base * W, rMin, rMax);
    auto& stream = formal ? formal->control : controlLog;
    stream << runId << ',' << vehicleId << ',';
    if (formal) stream << simTime().str() << ',' << SimTime(windowStart).str() << ',' << simTime().str();
    else stream << now << ',' << windowStart << ',' << now;
    stream << ',' << busyDuration << ',' << C << ',' << Cbar << ',' << innovation << ',' << brake << ','
               << S << ',' << U << ',' << base << ',' << rate << ',' << speed << ',' << W;
    if (formal) stream << ',' << longitudinalPosition();
    stream << '\n';
    windowStart = now;
    if (!formal) writeStates();
    if (hasSubmitted) rescheduleSend();
}

void TabrApp::rescheduleSend()
{
    if (sendTimer->isScheduled()) cancelEvent(sendTimer);
    const double next = nextSend(simTime().dbl(), lastSubmit, rate, jitter, rMin, rMax);
    scheduleAt(SimTime(next), sendTimer);
}

void TabrApp::transmit()
{
    const double now = simTime().dbl();
    if (hasSubmitted && now - lastSubmit < 1.0 / rMax - 1e-9)
        throw cRuntimeError("TABR send interval violated rMax");
    auto* packet = new TabrBeacon("TABR beacon");
    populateWSM(packet);
    packet->setBitLength(messageBits);
    packet->setSenderId(vehicleId.c_str());
    packet->setSeq(sequence);
    packet->setGenTime(simTime());
    packet->setPositionM(longitudinalPosition());
    packet->setSpeedMps(mobility->getSpeed());
    packet->setBrakeActive(brake);
    const bool activeEvent = eventId >= 0 && now <= eventUntil;
    packet->setEventId(activeEvent ? eventId : -1);
    packet->setEventDetectTime(SimTime(activeEvent ? eventDetected : -1));
    packet->setEventValidUntil(SimTime(activeEvent ? eventUntil : -1));
    if (!formal || logTx) {
        auto& stream = formal ? formal->tx : txLog;
        stream << runId << ',' << vehicleId << ',' << sequence << ',';
        if (formal) stream << packet->getGenTime().str() << ',' << simTime().str();
        else stream << now << ',' << now;
        stream << ',' << packet->getPositionM() << ',' << packet->getSpeedMps() << ',' << brake << ',' << packet->getEventId() << ',';
        if (formal) stream << packet->getEventDetectTime().str() << ',' << packet->getEventValidUntil().str();
        else stream << packet->getEventDetectTime().dbl() << ',' << packet->getEventValidUntil().dbl();
        stream << ',' << packet->getBitLength() << '\n';
        ++loggedTx;
    }
    sendDown(packet);
    ++sequence;
    lastSubmit = now;
    hasSubmitted = true;
    jitter = formal ? FormalObservation::jitterFor(vehicleId,formalSeed,sequence-1,jitterFraction)
                    : uniform(1 - jitterFraction, 1 + jitterFraction);
    rescheduleSend();
}

void TabrApp::onBSM(DemoSafetyMessage* beacon)
{
    auto* packet = dynamic_cast<TabrBeacon*>(beacon);
    if (!packet) return;
    const double now = simTime().dbl();
    const std::string sender = packet->getSenderId();
    ++rxCount;
    ensurePriors();
    State& state = states[sender];
    const double generated = packet->getGenTime().dbl();
    const bool validState = sender != vehicleId && generated <= now &&
        (!state.known || (packet->getSeq() > state.seq && generated >= state.generated));
    const bool validEvent = validState && packet->getEventId() >= 0 &&
        packet->getEventDetectTime() >= SIMTIME_ZERO &&
        packet->getGenTime() >= packet->getEventDetectTime() &&
        simTime() <= packet->getEventValidUntil();
    std::string reason;
    if (!validState) reason = "duplicate_or_out_of_order_state";
    else if (packet->getEventId() < 0) reason = "no_event";
    else if (!validEvent) reason = "expired_or_invalid_event";
    if (formal) {
        if (fullLog || observedSet.count(sender)) {
            formal->rx << runId << ',' << vehicleId << ',' << sender << ',' << packet->getSeq() << ',' << simTime().str() << ','
                       << packet->getGenTime().str() << ',' << packet->getPositionM() << ',' << packet->getSpeedMps() << ','
                       << packet->getBrakeActive() << ',' << packet->getEventId() << ',' << packet->getEventDetectTime().str() << ','
                       << packet->getEventValidUntil().str() << ',' << validState << ',' << validState << ',' << validEvent << ',' << reason << '\n';
            ++loggedRx;
        }
    } else rxLog << runId << ',' << vehicleId << ',' << sender << ',' << packet->getSeq() << ',' << now << ','
          << generated << ',' << packet->getEventId() << ',' << packet->getEventDetectTime().dbl() << ','
          << packet->getEventValidUntil().dbl() << ',' << packet->getPositionM() << ',' << packet->getSpeedMps()
          << ',' << packet->getBrakeActive() << ',' << validState << ',' << validEvent << ',' << reason << '\n';
    if (validState) {
        state.known = true;
        state.givenPrior = false;
        state.seq = packet->getSeq();
        state.generated = generated;
        state.x = packet->getPositionM();
        state.speed = packet->getSpeedMps();
    }
}

void TabrApp::ensurePriors()
{
    if (!formal || priorApplied || simTime() < warmup) return;
    for (const auto& sender : observedSenders) {
        const Prior* prior = formal->priorFor(sender,vehicleId);
        if (!prior) continue;
        if (prior->time != warmup) throw cRuntimeError("S0 prior must match formalWarmup");
        State value;
        value.known = value.givenPrior = true;
        value.seq = -1;
        value.generated = prior->time.dbl();
        value.x = prior->x;
        value.speed = prior->speed;
        states[sender] = value;
    }
    priorApplied = true;
}

void TabrApp::writeObservedStates()
{
    ensurePriors();
    const double now = simTime().dbl();
    for (const auto& sender : observedSenders) {
        const State& s = states.at(sender);
        auto& out = formal->state;
        out << runId << ',' << simTime().str() << ',' << vehicleId << ',' << sender << ',' << s.known;
        if (!s.known) { out << ",unknown,,,,,,\n"; continue; }
        out << (s.givenPrior ? ",given_prior," : ",received,");
        if (!s.givenPrior) out << s.seq;
        out << ',' << SimTime(s.generated).str() << ',' << s.x << ',' << s.speed << ','
            << s.x+s.speed*(now-s.generated) << ',' << (simTime()-SimTime(s.generated)).str() << '\n';
    }
}

void TabrApp::writeStates()
{
    const double now = simTime().dbl();
    for (const auto& item : states) {
        const State& state = item.second;
        stateLog << runId << ',' << now << ',' << vehicleId << ',' << item.first << ',' << state.known;
        if (state.known)
            stateLog << ",received," << state.seq << ',' << state.generated << ',' << state.x << ','
                     << state.speed << ',' << state.x + state.speed * (now - state.generated) << ','
                     << now - state.generated;
        else stateLog << ",unknown,,,,,,";
        stateLog << '\n';
    }
}

void TabrApp::finish()
{
    if (ready && formal && !lifecycleWritten) {
        formal->lifecycle << runId << ',' << vehicleId << ',' << enterTime.str() << ',' << simTime().str() << ','
                          << sequence << ',' << rxCount << ',' << logBusy << ',' << loggedTx << ',' << loggedRx << '\n';
        formal->vehicleLeft();
        lifecycleWritten = true;
    } else if (ready && !formal) {
        controlLog.flush(); txLog.flush(); rxLog.flush();
        truthLog.flush(); stateLog.flush(); busyLog.flush(); detectionLog.flush();
    }
    DemoBaseApplLayer::finish();
}

} // namespace tabr
