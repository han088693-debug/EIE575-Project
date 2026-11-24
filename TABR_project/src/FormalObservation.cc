#include "FormalObservation.h"
#include <algorithm>
#include <iomanip>
#include <limits>
#include <memory>
#include <sstream>
#include <stdexcept>

using namespace omnetpp;
namespace tabr {
namespace {
std::unique_ptr<FormalObservation> instance;
int externalLogs = 0, previousExternalPeak = 0;

void openLog(std::ofstream& file, const std::string& folder, const char* name, const char* header)
{
    file.exceptions(std::ios::badbit | std::ios::failbit);
    file.open(folder + "/" + name + ".csv");
    file << std::setprecision(17) << header << '\n';
}

std::vector<std::string> splitCsv(const std::string& row)
{
    std::vector<std::string> fields;
    std::string field;
    bool quoted = false;
    for (size_t i=0; i<row.size(); ++i) {
        const char c = row[i];
        if (c == '"') {
            if (quoted && i+1 < row.size() && row[i+1] == '"') { field += '"'; ++i; }
            else quoted = !quoted;
        }
        else if (c == ',' && !quoted) { fields.push_back(field); field.clear(); }
        else if (c != '\r') field += c;
    }
    if (quoted) throw cRuntimeError("Unclosed quote in formal input CSV");
    fields.push_back(field);
    return fields;
}

template<typename F>
void readCsv(const char* path, F fn)
{
    std::ifstream in(path);
    std::string line;
    if (!std::getline(in,line)) throw cRuntimeError("Missing formal input %s",path);
    if (line.compare(0,3,"\xEF\xBB\xBF") == 0) line.erase(0,3);
    auto header = splitCsv(line);
    while (std::getline(in,line)) {
        if (line.empty() || line == "\r") continue;
        auto fields = splitCsv(line);
        if (header.size() != fields.size()) throw cRuntimeError("Malformed row in %s",path);
        std::map<std::string,std::string> record;
        for (size_t i=0; i<header.size(); ++i) record[header[i]] = fields[i];
        fn(record);
    }
    if (!in.eof()) throw cRuntimeError("Failed reading %s",path);
}
}

FormalObservation::FormalObservation(const std::string& folder, const std::string& run) : runId(run)
{
    using Row = std::map<std::string,std::string>;
    readCsv("pairs.csv", [this](const Row& r) {
        const auto& s = r.at("sender_id"); const auto& v = r.at("receiver_id");
        receivers[v].push_back(s);
        senders.insert(s);
        audited.insert(v); // fixed target receivers and preregistered background receivers
        if (r.at("role") == "target") { audited.insert(s); targets.insert(s); targets.insert(v); }
    });
    readCsv("prior.csv", [this](const Row& r) {
        const auto pair = std::make_pair(r.at("sender_id"),r.at("receiver_id"));
        Prior p{SimTime::parse(r.at("init_time").c_str()),std::stod(r.at("position_m")),std::stod(r.at("speed_mps"))};
        if (!priors.emplace(pair,p).second) throw cRuntimeError("Duplicate registered prior");
    });
    readCsv("phases.csv", [this](const Row& r) {
        if (!phases.emplace(r.at("vehicle_id"),std::stod(r.at("phase_s"))).second)
            throw cRuntimeError("Duplicate vehicle in phases.csv");
    });
    openLog(control,folder,"control_log","run_id,vehicle_id,tick_time,window_start,window_end,busy_duration_s,C,Cbar,e_H,B,S,U,b,r,speed_mps,W,lane_position_m");
    openLog(tx,folder,"tx_log","run_id,sender_id,seq,t_gen,t_submit,position_m,speed_mps,brake_active,event_id,event_detect_time,event_valid_until,message_bits");
    openLog(rx,folder,"rx_log","run_id,receiver_id,sender_id,seq,t_rx,t_gen,position_m,speed_mps,brake_active,event_id,event_detect_time,event_valid_until,state_valid,state_accepted,event_valid,invalid_reason");
    openLog(busy,folder,"busy_audit","run_id,vehicle_id,transition_time,busy_state");
    openLog(state,folder,"state_log","run_id,t,receiver_id,sender_id,known,state_source,last_seq,last_state_gen_time,p_last_m,v_last_mps,p_hat_m,Age_s");
    openLog(detection,folder,"detection_log","run_id,sender_id,event_id,t_detect,event_valid_until");
    openLog(lifecycle,folder,"lifecycle","run_id,vehicle_id,enter_s,exit_s,tx_count,rx_count,busy_audited,logged_tx_count,logged_rx_count");
    openLog(counterLog,folder,"engine_counters","t_sim_s,active_vehicle_count,open_log_streams,peak_open_log_streams");
    peak = previousExternalPeak;
    counters();
}

FormalObservation& FormalObservation::open(const std::string& folder, const std::string& run)
{
    if (!instance) instance.reset(new FormalObservation(folder,run));
    if (instance->runId != run) throw cRuntimeError("Use one process per isolated formal run");
    return *instance;
}
void FormalObservation::scenarioStreams(int n)
{
    if (n < 0) throw cRuntimeError("Negative scenario stream count");
    externalLogs = n;
    previousExternalPeak = std::max(previousExternalPeak,n);
    if (instance) instance->counters();
}
void FormalObservation::counters()
{
    const int count = externalLogs + 8;
    peak = std::max(peak,count);
    counterLog << simTime().str() << ',' << active << ',' << count << ',' << peak << '\n';
}
void FormalObservation::vehicleEntered() { ++active; counters(); }
void FormalObservation::vehicleLeft()
{
    if (!active) throw cRuntimeError("Unmatched vehicle departure");
    --active;
    counters();
}
const std::vector<std::string>& FormalObservation::observedBy(const std::string& receiver) const
{
    static const std::vector<std::string> empty;
    const auto it = receivers.find(receiver);
    return it == receivers.end() ? empty : it->second;
}
const Prior* FormalObservation::priorFor(const std::string& sender, const std::string& receiver) const
{
    const auto it = priors.find(std::make_pair(sender,receiver));
    return it == priors.end() ? nullptr : &it->second;
}
double FormalObservation::phase(const std::string& id) const
{
    const auto it = phases.find(id);
    if (it == phases.end()) throw cRuntimeError("Missing phase for %s",id.c_str());
    return it->second;
}
double FormalObservation::jitterFor(const std::string& vehicle, int seed, long index, double fraction)
{
    // FNV-1a over stable ASCII coordinates, followed by the SplitMix64 finalizer.
    const std::string key = "jitter/" + std::to_string(seed) + "/" + vehicle + "/" + std::to_string(index);
    uint64_t x = UINT64_C(14695981039346656037);
    for (unsigned char c : key) { x ^= c; x *= UINT64_C(1099511628211); }
    x ^= x >> 30; x *= UINT64_C(0xbf58476d1ce4e5b9);
    x ^= x >> 27; x *= UINT64_C(0x94d049bb133111eb);
    x ^= x >> 31;
    const double u = static_cast<double>(x >> 11) / 9007199254740992.0;
    return 1 - fraction + 2*fraction*u;
}
}
