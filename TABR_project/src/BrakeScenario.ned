package tabr;

simple BrakeScenario
{
    parameters:
        @class(tabr::BrakeScenario);
        @display("i=block/control");
        string outputDir;
        string runId;
        bool formalMode = default(false);
        string formalScenario = default("S1");
        double formalWarmup @unit(s) = default(20s);
        double formalLocalRadius @unit(m) = default(1000m);
        string leadId = default("lead");
        string followId = default("follow");
        double brakeTime @unit(s) = default(20s);
        double targetSpeed @unit(mps) = default(0mps);
        double brakeDuration @unit(s) = default(3.7037s);
        double updateInterval @unit(s) = default(0.1s);
        double deadline @unit(s) = default(0.8s);
        double observationWindow @unit(s) = default(2s);
}
