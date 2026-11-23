package tabr;

import org.car2x.veins.nodes.Scenario;

network ResearchHighway extends Scenario
{
    submodules:
        brakeScenario: BrakeScenario {
            @display("p=700,80;i=block/control");
        }
}
