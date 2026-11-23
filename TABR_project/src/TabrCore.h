#pragma once

#include <algorithm>
#include <cmath>
#include <deque>
#include <stdexcept>
#include <string>

namespace tabr {

inline double clip(double x, double low, double high)
{
    return std::max(low, std::min(x, high));
}

struct MotionSample {
    double t;
    double x;
    double speed;
};

class MotionHistory {
public:
    explicit MotionHistory(double lookback) : lookback(lookback) {}

    double push(double t, double x, double speed)
    {
        double accel = 0;
        if (!samples.empty()) {
            if (t < samples.back().t) throw std::invalid_argument("motion time moved backwards");
            if (t == samples.back().t) {
                samples.back() = {t, x, speed};
                return lastAccel;
            }
            accel = (speed - samples.back().speed) / (t - samples.back().t);
        }
        samples.push_back({t, x, speed});
        while (samples.size() > 2 && samples[1].t <= t - lookback) samples.pop_front();
        lastAccel = accel;
        return accel;
    }

    double innovation() const
    {
        if (samples.size() < 2) return 0;
        const double target = samples.back().t - lookback;
        if (target < samples.front().t - 1e-9) return 0;
        for (std::size_t i = 1; i < samples.size(); ++i) {
            if (samples[i].t + 1e-9 < target) continue;
            const auto& a = samples[i - 1];
            const auto& b = samples[i];
            const double ratio = clip((target - a.t) / (b.t - a.t), 0, 1);
            const double x = a.x + ratio * (b.x - a.x);
            const double v = a.speed + ratio * (b.speed - a.speed);
            return std::abs(samples.back().x - (x + v * lookback));
        }
        return 0;
    }

private:
    double lookback;
    double lastAccel = 0;
    std::deque<MotionSample> samples;
};

class BusyWindow {
public:
    explicit BusyWindow(double start) : lastTime(start) {}

    void advance(double now)
    {
        if (now < lastTime) throw std::invalid_argument("CBR time moved backwards");
        if (busy) duration += now - lastTime;
        lastTime = now;
    }

    void change(double now, bool nextBusy)
    {
        advance(now);
        busy = nextBusy;
    }

    double take(double now)
    {
        advance(now);
        const double result = duration;
        duration = 0;
        return result;
    }

private:
    double lastTime;
    double duration = 0;
    bool busy = false;
};

inline double weight(const std::string& scheme, bool brake, double score, double speed,
    double speedReference, double beta)
{
    if (scheme == "G0") return 1;
    if (scheme == "G1") return clip(speed / speedReference, 1, 1 + beta);
    if (scheme == "G2") return 1 + beta * (brake ? 1 : 0);
    if (scheme == "G3") return 1 + beta * std::max(brake ? 1.0 : 0.0, score);
    throw std::invalid_argument("scheme must be G0, G1, G2 or G3");
}

inline double updateBase(double base, double cbar, double target, double deadband,
    double eta, double minimum, double maximum)
{
    const double error = target - cbar;
    return std::abs(error) <= deadband + 1e-12 ? base : clip(base + eta * error, minimum, maximum);
}

// 控制律只允许在估计量可用后积分。仿真开始时窗口内还没有流量，CBR 估计接近 0，
// 若立即积分会被当成真实欠载而把 base 推到上限（启动饱和）。
// 估计量的有效时间取一个控制窗口量级；场景预热只用于测量，不用于控制门控。
constexpr double kEstimateSettleSeconds = 1.0;

inline bool controlActive(double now, double settleSeconds, bool estimateValid)
{
    return estimateValid && now + 1e-12 >= settleSeconds;
}

inline double sendInterval(double rate, double jitter, double minimumRate, double maximumRate)
{
    return clip(jitter / rate, 1.0 / maximumRate, 1.0 / minimumRate);
}

inline double nextSend(double now, double lastSubmit, double rate, double jitter,
    double minimumRate, double maximumRate)
{
    return std::max(now, lastSubmit + sendInterval(rate, jitter, minimumRate, maximumRate));
}

} // namespace tabr
