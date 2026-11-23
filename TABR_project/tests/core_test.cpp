#include "../src/TabrCore.h"

#include <cmath>
#include <iostream>
#include <stdexcept>
#include <string>

namespace {
int checks = 0;
int failures = 0;

void expect(bool ok, const std::string& name)
{
    ++checks;
    if (!ok) {
        ++failures;
        std::cerr << "FAIL: " << name << '\n';
    }
}

void near(double actual, double expected, const std::string& name, double tolerance = 1e-10)
{
    expect(std::isfinite(actual) && std::abs(actual - expected) <= tolerance, name);
    if (!std::isfinite(actual) || std::abs(actual - expected) > tolerance)
        std::cerr << "  actual=" << actual << " expected=" << expected << '\n';
}

void busyWindows()
{
    tabr::BusyWindow busy(0);
    busy.change(0.2, true);
    near(busy.take(0.5), 0.3, "busy segment before first window boundary");
    near(busy.take(1.0), 0.5, "busy state survives take across a full window");
    busy.change(1.2, false);
    near(busy.take(1.5), 0.2, "busy segment after a window boundary");
    near(busy.take(1.5), 0, "same-time take cannot double-count busy time");
    busy.change(1.6, true);
    busy.change(1.7, true);
    near(busy.take(2.0), 0.4, "duplicate busy notification cannot double-count");
    busy.change(2.0, false);
    near(busy.take(2.5), 0, "idle window");
    bool rejected = false;
    try { busy.advance(2.4); } catch (const std::invalid_argument&) { rejected = true; }
    expect(rejected, "reject time reversal in CBR integration");

    tabr::BusyWindow signalFirst(0), tickFirst(0);
    signalFirst.change(0.5, true);
    near(signalFirst.take(0.5), 0, "boundary signal has zero prior duration");
    near(tickFirst.take(0.5), 0, "boundary tick has zero prior duration");
    tickFirst.change(0.5, true);
    near(signalFirst.take(1), tickFirst.take(1), "same-time signal/tick ordering preserves area");
}

void motionHistory()
{
    tabr::MotionHistory cruise(0.5);
    const double times[] = {0, 0.12, 0.34, 0.5, 0.77, 1.0, 1.13, 1.5, 1.94, 2.1};
    for (double t : times) {
        near(cruise.push(t, 100 + 20 * t, 20), 0, "constant-speed acceleration");
        near(cruise.innovation(), 0, "constant speed gives zero fixed-H innovation");
    }

    tabr::MotionHistory braking(0.5);
    for (int step = 0; step <= 20; ++step) {
        const double t = step * 0.1;
        const double a = braking.push(t, 100 + 22 * t - 3 * t * t, 22 - 6 * t);
        near(a, step ? -6 : 0, "finite-difference braking acceleration");
        near(braking.innovation(), step >= 5 ? 0.75 : 0,
            "fixed 0.5 s lookback survives pruning and gives 0.75 m brake innovation");
    }

    tabr::MotionHistory interpolation(0.5);
    for (double t : {0.0, 0.2, 0.4, 0.8})
        interpolation.push(t, 100 + 22 * t - 3 * t * t, 22 - 6 * t);
    // At t=0.8, the t=0.3 state interpolates to x=106.30, v=20.2.
    // x(0.8)=115.68, so |115.68-(106.30+20.2*0.5)|=0.72.
    near(interpolation.innovation(), 0.72, "interpolate x and v at the fixed-H target");

    bool rejected = false;
    try { interpolation.push(0.7, 0, 0); } catch (const std::invalid_argument&) { rejected = true; }
    expect(rejected, "reject motion time reversal");
}

void controller()
{
    near(tabr::updateBase(2, 0.4, 0.6, 0.03, 0.5, 1.0 / 3, 10), 2.1, "below-target CBR raises base");
    near(tabr::updateBase(2, 0.8, 0.6, 0.03, 0.5, 1.0 / 3, 10), 1.9, "above-target CBR lowers base");
    near(tabr::updateBase(2, 0.59, 0.6, 0.03, 0.5, 1.0 / 3, 10), 2, "hold inside deadband");
    near(tabr::updateBase(2, 0.375, 0.5, 0.125, 0.5, 1.0 / 3, 10), 2, "hold on lower deadband boundary");
    near(tabr::updateBase(2, 0.625, 0.5, 0.125, 0.5, 1.0 / 3, 10), 2, "hold on upper deadband boundary");
    near(tabr::updateBase(2, 0.57, 0.6, 0.03, 0.5, 1.0 / 3, 10), 2, "decimal lower deadband boundary");
    near(tabr::updateBase(2, 0.63, 0.6, 0.03, 0.5, 1.0 / 3, 10), 2, "decimal upper deadband boundary");
    near(tabr::updateBase(2, 0.57 - 1e-8, 0.6, 0.03, 0.5, 1.0 / 3, 10),
        2 + 0.5 * (0.03 + 1e-8), "update just outside lower decimal deadband");
    near(tabr::updateBase(2, 0.63 + 1e-8, 0.6, 0.03, 0.5, 1.0 / 3, 10),
        2 - 0.5 * (0.03 + 1e-8), "update just outside upper decimal deadband");
    near(tabr::updateBase(2, 0, 0.6, 0.03, 100, 1.0 / 3, 10), 10, "base upper bound");
    near(tabr::updateBase(2, 1, 0.6, 0.03, 100, 1.0 / 3, 10), 1.0 / 3, "base lower bound");

    const double speedReference = 33.0 / 3.6;
    near(tabr::weight("G0", true, 1, 20, speedReference, 2), 1, "G0 has unit multiplier");
    near(tabr::weight("G2", false, 1, 20, speedReference, 2), 1, "G2 ignores innovation");
    near(tabr::weight("G2", true, 0, 0, speedReference, 2), 3, "G2 brake multiplier unchanged");
    near(tabr::weight("G3", false, 0.3, 0, speedReference, 2), 1.6, "G3 innovation multiplier unchanged");
    near(tabr::weight("G3", true, 0.3, 0, speedReference, 2), 3, "G3 brake dominates innovation");

    const double speedKph[] = {0, 16.5, 33, 66, 80, 99, 120, 198};
    const double expectedWeight[] = {1, 1, 1, 2, 80.0 / 33, 3, 3, 3};
    for (int i = 0; i < 8; ++i) {
        const double actual = tabr::weight("G1", false, 0, speedKph[i] / 3.6, speedReference, 2);
        near(actual, expectedWeight[i], "G1 speed reference and multiplier bounds");
        near(tabr::weight("G1", true, 1, speedKph[i] / 3.6, speedReference, 2), actual,
            "G1 ignores braking and innovation");
    }
    near(2 * tabr::weight("G1", false, 0, 33.0 / 3.6, speedReference, 2), 2,
        "reference speed does not receive an extra 1+beta transform");
    near(2 * tabr::weight("G1", false, 0, 66.0 / 3.6, speedReference, 2), 4,
        "double reference speed doubles the base rate");
    near(2 * tabr::weight("G1", false, 0, 80.0 / 3.6, speedReference, 2), 160.0 / 33,
        "80 km/h remains below the weight cap");
    near(2 * tabr::weight("G1", false, 0, 120.0 / 3.6, speedReference, 2), 6,
        "120 km/h has a distinct bounded rate");
    near(tabr::weight("G1", false, 0, 120.0 / 3.6, speedReference, 1), 2,
        "beta=1 lowers the G1 cap");
    near(tabr::weight("G1", false, 0, 120.0 / 3.6, speedReference, 3), 120.0 / 33,
        "beta=3 allows the unsaturated speed ratio");
    for (const std::string scheme : {"G0", "G1", "G2", "G3"}) {
        const double lowBase = 1.0 / 3;
        near(tabr::clip(lowBase * tabr::weight(scheme, false, 0, 0, speedReference, 2), 1, 10), 1,
            scheme + " respects minimum rate");
        near(tabr::clip(lowBase * tabr::weight(scheme, true, 1, 40, speedReference, 2), 1, 10), 1,
            scheme + " can reach minimum rate even at maximum priority");
        near(tabr::clip(10 * tabr::weight(scheme, true, 1, 40, speedReference, 2), 1, 10), 10,
            scheme + " respects maximum rate");
        near(tabr::weight(scheme, true, 1, 40, speedReference, 0), 1,
            scheme + " has no priority multiplier at beta=0");
    }
    bool rejected = false;
    try { tabr::weight("G4", false, 0, 0, speedReference, 2); } catch (const std::invalid_argument&) { rejected = true; }
    expect(rejected, "reject unknown group");
}

void sendTiming()
{
    near(tabr::sendInterval(10, 0.95, 1, 10), 0.1, "jitter cannot exceed maximum send rate");
    near(tabr::sendInterval(1, 1.05, 1, 10), 1, "jitter cannot violate minimum send rate");
    near(tabr::sendInterval(2, 1.05, 1, 10), 0.525, "interior jitter is retained");
    near(tabr::nextSend(0.4, 0, 2, 1, 1, 10), 0.5, "deadline anchors to last submission");
    near(tabr::nextSend(0.5, 0, 2, 1, 1, 10), 0.5, "control at deadline leaves send due now");
    near(tabr::nextSend(0.8, 0, 2, 1, 1, 10), 0.8, "overdue transmission is due immediately");

    // A small event loop models control-before-send ties and repeated cancellation.
    // Controls run every 0.05 s, faster than any permitted transmission interval.
    double last = 0, rate = 1, jitter = 1, due = 1;
    int tick = 1, sent = 0;
    const double rates[] = {1, 10, 1, 2, 1, 5, 1};
    const double jitters[] = {0.95, 1.05, 1};
    while (std::min(tick * 0.05, due) <= 20.0) {
        const double controlAt = tick * 0.05;
        if (controlAt <= due + 1e-12) {
            rate = rates[tick % 7];
            due = tabr::nextSend(controlAt, last, rate, jitter, 1, 10);
            expect(due <= last + 1 + 1e-10, "rescheduling cannot extend the hard latest-send bound");
            ++tick;
        }
        else {
            expect(due - last >= 0.1 - 1e-10, "actual submission spacing respects rMax");
            expect(due - last <= 1 + 1e-10, "actual submission spacing respects rMin");
            last = due;
            jitter = jitters[sent % 3];
            ++sent;
            due = tabr::nextSend(last, last, rate, jitter, 1, 10);
        }
        if (tick + sent > 10000) {
            expect(false, "event loop must make progress");
            break;
        }
    }
    expect(sent >= 20, "frequent control timers cannot starve sends");
    expect(last >= 19, "submissions continue to the end of the 20 s test");
}
}

void warmupGate()
{
    expect(!tabr::controlActive(0.1, 20.0, true), "gate closed before warmup");
    expect(tabr::controlActive(20.0, 20.0, true), "gate opens at warmup");
    expect(tabr::controlActive(59.9, 20.0, true), "gate open after warmup");
    expect(!tabr::controlActive(59.9, 20.0, false), "gate closed without valid estimate");

    double base = 2;
    for (double t = 0.1; t < 20.0; t += 0.1) {
        if (tabr::controlActive(t, 20.0, true))
            base = tabr::updateBase(base, 0.0, 0.43, 0.03, 0.5, 1.0 / 3, 10);
    }
    near(base, 2, "base held through warmup despite zero CBR");
    if (tabr::controlActive(20.1, 20.0, true))
        base = tabr::updateBase(base, 0.408, 0.43, 0.03, 0.5, 1.0 / 3, 10);
    near(base, 2, "base held when measured CBR sits inside the deadband");
}

int main()
{
    busyWindows();
    motionHistory();
    controller();
    warmupGate();
    sendTiming();
    std::cout << (failures ? "FAIL" : "PASS") << ": " << checks << " checks, " << failures << " failures\n";
    return failures ? 1 : 0;
}
