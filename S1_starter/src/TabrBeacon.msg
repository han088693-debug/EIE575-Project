cplusplus {{
#include "veins/modules/messages/DemoSafetyMessage_m.h"
}}

namespace veins;
class DemoSafetyMessage;

packet TabrBeacon extends DemoSafetyMessage
{
    string senderId;
    long seq;
    simtime_t genTime;
    double positionM;
    double speedMps;
    bool brakeActive;
    long eventId = -1;
    simtime_t eventDetectTime = -1;
    simtime_t eventValidUntil = -1;
}
