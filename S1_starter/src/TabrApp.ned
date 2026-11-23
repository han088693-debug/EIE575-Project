package tabr;

import org.car2x.veins.modules.application.ieee80211p.DemoBaseApplLayer;

simple TabrApp extends DemoBaseApplLayer
{
    parameters:
        @class(tabr::TabrApp);
        sendBeacons = false;
        avoidBeaconSynchronization = false;
        dataOnSch = false;
        string scheme = default("G0");
        string outputDir;
        string runId;
        string targetId = default("lead");
        double controlPeriod @unit(s) = default(0.1s);
        double lookback @unit(s) = default(0.5s);
        double eventValidity @unit(s) = default(2s);
        double brakeThreshold @unit(mpss) = default(4mpss);
        double errorScale @unit(m) = default(0.5m);
        double speedReference @unit(mps) = default(9.166666666666667mps); // 33 km/h
        double rMinHz = default(1);
        double rMaxHz = default(10);
        double initialBaseHz = default(2);
        double beta = default(2);
        double cbrTarget = default(0.60);
        double cbrDeadband = default(0.03);
        double feedbackGain = default(0.5);
        double smoothingOld = default(0.8);
        double jitterFraction = default(0.05);
        int messageBits @unit(bit) = default(2000bit);
}
