//
// Generated file, do not edit! Created by nedtool 5.6 from src/TabrBeacon.msg.
//

// Disable warnings about unused variables, empty switch stmts, etc:
#ifdef _MSC_VER
#  pragma warning(disable:4101)
#  pragma warning(disable:4065)
#endif

#if defined(__clang__)
#  pragma clang diagnostic ignored "-Wshadow"
#  pragma clang diagnostic ignored "-Wconversion"
#  pragma clang diagnostic ignored "-Wunused-parameter"
#  pragma clang diagnostic ignored "-Wc++98-compat"
#  pragma clang diagnostic ignored "-Wunreachable-code-break"
#  pragma clang diagnostic ignored "-Wold-style-cast"
#elif defined(__GNUC__)
#  pragma GCC diagnostic ignored "-Wshadow"
#  pragma GCC diagnostic ignored "-Wconversion"
#  pragma GCC diagnostic ignored "-Wunused-parameter"
#  pragma GCC diagnostic ignored "-Wold-style-cast"
#  pragma GCC diagnostic ignored "-Wsuggest-attribute=noreturn"
#  pragma GCC diagnostic ignored "-Wfloat-conversion"
#endif

#include <iostream>
#include <sstream>
#include "TabrBeacon_m.h"

namespace omnetpp {

// Template pack/unpack rules. They are declared *after* a1l type-specific pack functions for multiple reasons.
// They are in the omnetpp namespace, to allow them to be found by argument-dependent lookup via the cCommBuffer argument

// Packing/unpacking an std::vector
template<typename T, typename A>
void doParsimPacking(omnetpp::cCommBuffer *buffer, const std::vector<T,A>& v)
{
    int n = v.size();
    doParsimPacking(buffer, n);
    for (int i = 0; i < n; i++)
        doParsimPacking(buffer, v[i]);
}

template<typename T, typename A>
void doParsimUnpacking(omnetpp::cCommBuffer *buffer, std::vector<T,A>& v)
{
    int n;
    doParsimUnpacking(buffer, n);
    v.resize(n);
    for (int i = 0; i < n; i++)
        doParsimUnpacking(buffer, v[i]);
}

// Packing/unpacking an std::list
template<typename T, typename A>
void doParsimPacking(omnetpp::cCommBuffer *buffer, const std::list<T,A>& l)
{
    doParsimPacking(buffer, (int)l.size());
    for (typename std::list<T,A>::const_iterator it = l.begin(); it != l.end(); ++it)
        doParsimPacking(buffer, (T&)*it);
}

template<typename T, typename A>
void doParsimUnpacking(omnetpp::cCommBuffer *buffer, std::list<T,A>& l)
{
    int n;
    doParsimUnpacking(buffer, n);
    for (int i=0; i<n; i++) {
        l.push_back(T());
        doParsimUnpacking(buffer, l.back());
    }
}

// Packing/unpacking an std::set
template<typename T, typename Tr, typename A>
void doParsimPacking(omnetpp::cCommBuffer *buffer, const std::set<T,Tr,A>& s)
{
    doParsimPacking(buffer, (int)s.size());
    for (typename std::set<T,Tr,A>::const_iterator it = s.begin(); it != s.end(); ++it)
        doParsimPacking(buffer, *it);
}

template<typename T, typename Tr, typename A>
void doParsimUnpacking(omnetpp::cCommBuffer *buffer, std::set<T,Tr,A>& s)
{
    int n;
    doParsimUnpacking(buffer, n);
    for (int i=0; i<n; i++) {
        T x;
        doParsimUnpacking(buffer, x);
        s.insert(x);
    }
}

// Packing/unpacking an std::map
template<typename K, typename V, typename Tr, typename A>
void doParsimPacking(omnetpp::cCommBuffer *buffer, const std::map<K,V,Tr,A>& m)
{
    doParsimPacking(buffer, (int)m.size());
    for (typename std::map<K,V,Tr,A>::const_iterator it = m.begin(); it != m.end(); ++it) {
        doParsimPacking(buffer, it->first);
        doParsimPacking(buffer, it->second);
    }
}

template<typename K, typename V, typename Tr, typename A>
void doParsimUnpacking(omnetpp::cCommBuffer *buffer, std::map<K,V,Tr,A>& m)
{
    int n;
    doParsimUnpacking(buffer, n);
    for (int i=0; i<n; i++) {
        K k; V v;
        doParsimUnpacking(buffer, k);
        doParsimUnpacking(buffer, v);
        m[k] = v;
    }
}

// Default pack/unpack function for arrays
template<typename T>
void doParsimArrayPacking(omnetpp::cCommBuffer *b, const T *t, int n)
{
    for (int i = 0; i < n; i++)
        doParsimPacking(b, t[i]);
}

template<typename T>
void doParsimArrayUnpacking(omnetpp::cCommBuffer *b, T *t, int n)
{
    for (int i = 0; i < n; i++)
        doParsimUnpacking(b, t[i]);
}

// Default rule to prevent compiler from choosing base class' doParsimPacking() function
template<typename T>
void doParsimPacking(omnetpp::cCommBuffer *, const T& t)
{
    throw omnetpp::cRuntimeError("Parsim error: No doParsimPacking() function for type %s", omnetpp::opp_typename(typeid(t)));
}

template<typename T>
void doParsimUnpacking(omnetpp::cCommBuffer *, T& t)
{
    throw omnetpp::cRuntimeError("Parsim error: No doParsimUnpacking() function for type %s", omnetpp::opp_typename(typeid(t)));
}

}  // namespace omnetpp

namespace veins {

// forward
template<typename T, typename A>
std::ostream& operator<<(std::ostream& out, const std::vector<T,A>& vec);

// Template rule which fires if a struct or class doesn't have operator<<
template<typename T>
inline std::ostream& operator<<(std::ostream& out,const T&) {return out;}

// operator<< for std::vector<T>
template<typename T, typename A>
inline std::ostream& operator<<(std::ostream& out, const std::vector<T,A>& vec)
{
    out.put('{');
    for(typename std::vector<T,A>::const_iterator it = vec.begin(); it != vec.end(); ++it)
    {
        if (it != vec.begin()) {
            out.put(','); out.put(' ');
        }
        out << *it;
    }
    out.put('}');
    
    char buf[32];
    sprintf(buf, " (size=%u)", (unsigned int)vec.size());
    out.write(buf, strlen(buf));
    return out;
}

Register_Class(TabrBeacon)

TabrBeacon::TabrBeacon(const char *name, short kind) : ::veins::DemoSafetyMessage(name,kind)
{
    this->seq = 0;
    this->genTime = 0;
    this->positionM = 0;
    this->speedMps = 0;
    this->brakeActive = false;
    this->eventId = -1;
    this->eventDetectTime = -1;
    this->eventValidUntil = -1;
}

TabrBeacon::TabrBeacon(const TabrBeacon& other) : ::veins::DemoSafetyMessage(other)
{
    copy(other);
}

TabrBeacon::~TabrBeacon()
{
}

TabrBeacon& TabrBeacon::operator=(const TabrBeacon& other)
{
    if (this==&other) return *this;
    ::veins::DemoSafetyMessage::operator=(other);
    copy(other);
    return *this;
}

void TabrBeacon::copy(const TabrBeacon& other)
{
    this->senderId = other.senderId;
    this->seq = other.seq;
    this->genTime = other.genTime;
    this->positionM = other.positionM;
    this->speedMps = other.speedMps;
    this->brakeActive = other.brakeActive;
    this->eventId = other.eventId;
    this->eventDetectTime = other.eventDetectTime;
    this->eventValidUntil = other.eventValidUntil;
}

void TabrBeacon::parsimPack(omnetpp::cCommBuffer *b) const
{
    ::veins::DemoSafetyMessage::parsimPack(b);
    doParsimPacking(b,this->senderId);
    doParsimPacking(b,this->seq);
    doParsimPacking(b,this->genTime);
    doParsimPacking(b,this->positionM);
    doParsimPacking(b,this->speedMps);
    doParsimPacking(b,this->brakeActive);
    doParsimPacking(b,this->eventId);
    doParsimPacking(b,this->eventDetectTime);
    doParsimPacking(b,this->eventValidUntil);
}

void TabrBeacon::parsimUnpack(omnetpp::cCommBuffer *b)
{
    ::veins::DemoSafetyMessage::parsimUnpack(b);
    doParsimUnpacking(b,this->senderId);
    doParsimUnpacking(b,this->seq);
    doParsimUnpacking(b,this->genTime);
    doParsimUnpacking(b,this->positionM);
    doParsimUnpacking(b,this->speedMps);
    doParsimUnpacking(b,this->brakeActive);
    doParsimUnpacking(b,this->eventId);
    doParsimUnpacking(b,this->eventDetectTime);
    doParsimUnpacking(b,this->eventValidUntil);
}

const char * TabrBeacon::getSenderId() const
{
    return this->senderId.c_str();
}

void TabrBeacon::setSenderId(const char * senderId)
{
    this->senderId = senderId;
}

long TabrBeacon::getSeq() const
{
    return this->seq;
}

void TabrBeacon::setSeq(long seq)
{
    this->seq = seq;
}

::omnetpp::simtime_t TabrBeacon::getGenTime() const
{
    return this->genTime;
}

void TabrBeacon::setGenTime(::omnetpp::simtime_t genTime)
{
    this->genTime = genTime;
}

double TabrBeacon::getPositionM() const
{
    return this->positionM;
}

void TabrBeacon::setPositionM(double positionM)
{
    this->positionM = positionM;
}

double TabrBeacon::getSpeedMps() const
{
    return this->speedMps;
}

void TabrBeacon::setSpeedMps(double speedMps)
{
    this->speedMps = speedMps;
}

bool TabrBeacon::getBrakeActive() const
{
    return this->brakeActive;
}

void TabrBeacon::setBrakeActive(bool brakeActive)
{
    this->brakeActive = brakeActive;
}

long TabrBeacon::getEventId() const
{
    return this->eventId;
}

void TabrBeacon::setEventId(long eventId)
{
    this->eventId = eventId;
}

::omnetpp::simtime_t TabrBeacon::getEventDetectTime() const
{
    return this->eventDetectTime;
}

void TabrBeacon::setEventDetectTime(::omnetpp::simtime_t eventDetectTime)
{
    this->eventDetectTime = eventDetectTime;
}

::omnetpp::simtime_t TabrBeacon::getEventValidUntil() const
{
    return this->eventValidUntil;
}

void TabrBeacon::setEventValidUntil(::omnetpp::simtime_t eventValidUntil)
{
    this->eventValidUntil = eventValidUntil;
}

class TabrBeaconDescriptor : public omnetpp::cClassDescriptor
{
  private:
    mutable const char **propertynames;
  public:
    TabrBeaconDescriptor();
    virtual ~TabrBeaconDescriptor();

    virtual bool doesSupport(omnetpp::cObject *obj) const override;
    virtual const char **getPropertyNames() const override;
    virtual const char *getProperty(const char *propertyname) const override;
    virtual int getFieldCount() const override;
    virtual const char *getFieldName(int field) const override;
    virtual int findField(const char *fieldName) const override;
    virtual unsigned int getFieldTypeFlags(int field) const override;
    virtual const char *getFieldTypeString(int field) const override;
    virtual const char **getFieldPropertyNames(int field) const override;
    virtual const char *getFieldProperty(int field, const char *propertyname) const override;
    virtual int getFieldArraySize(void *object, int field) const override;

    virtual const char *getFieldDynamicTypeString(void *object, int field, int i) const override;
    virtual std::string getFieldValueAsString(void *object, int field, int i) const override;
    virtual bool setFieldValueAsString(void *object, int field, int i, const char *value) const override;

    virtual const char *getFieldStructName(int field) const override;
    virtual void *getFieldStructValuePointer(void *object, int field, int i) const override;
};

Register_ClassDescriptor(TabrBeaconDescriptor)

TabrBeaconDescriptor::TabrBeaconDescriptor() : omnetpp::cClassDescriptor("veins::TabrBeacon", "veins::DemoSafetyMessage")
{
    propertynames = nullptr;
}

TabrBeaconDescriptor::~TabrBeaconDescriptor()
{
    delete[] propertynames;
}

bool TabrBeaconDescriptor::doesSupport(omnetpp::cObject *obj) const
{
    return dynamic_cast<TabrBeacon *>(obj)!=nullptr;
}

const char **TabrBeaconDescriptor::getPropertyNames() const
{
    if (!propertynames) {
        static const char *names[] = {  nullptr };
        omnetpp::cClassDescriptor *basedesc = getBaseClassDescriptor();
        const char **basenames = basedesc ? basedesc->getPropertyNames() : nullptr;
        propertynames = mergeLists(basenames, names);
    }
    return propertynames;
}

const char *TabrBeaconDescriptor::getProperty(const char *propertyname) const
{
    omnetpp::cClassDescriptor *basedesc = getBaseClassDescriptor();
    return basedesc ? basedesc->getProperty(propertyname) : nullptr;
}

int TabrBeaconDescriptor::getFieldCount() const
{
    omnetpp::cClassDescriptor *basedesc = getBaseClassDescriptor();
    return basedesc ? 9+basedesc->getFieldCount() : 9;
}

unsigned int TabrBeaconDescriptor::getFieldTypeFlags(int field) const
{
    omnetpp::cClassDescriptor *basedesc = getBaseClassDescriptor();
    if (basedesc) {
        if (field < basedesc->getFieldCount())
            return basedesc->getFieldTypeFlags(field);
        field -= basedesc->getFieldCount();
    }
    static unsigned int fieldTypeFlags[] = {
        FD_ISEDITABLE,
        FD_ISEDITABLE,
        FD_ISEDITABLE,
        FD_ISEDITABLE,
        FD_ISEDITABLE,
        FD_ISEDITABLE,
        FD_ISEDITABLE,
        FD_ISEDITABLE,
        FD_ISEDITABLE,
    };
    return (field>=0 && field<9) ? fieldTypeFlags[field] : 0;
}

const char *TabrBeaconDescriptor::getFieldName(int field) const
{
    omnetpp::cClassDescriptor *basedesc = getBaseClassDescriptor();
    if (basedesc) {
        if (field < basedesc->getFieldCount())
            return basedesc->getFieldName(field);
        field -= basedesc->getFieldCount();
    }
    static const char *fieldNames[] = {
        "senderId",
        "seq",
        "genTime",
        "positionM",
        "speedMps",
        "brakeActive",
        "eventId",
        "eventDetectTime",
        "eventValidUntil",
    };
    return (field>=0 && field<9) ? fieldNames[field] : nullptr;
}

int TabrBeaconDescriptor::findField(const char *fieldName) const
{
    omnetpp::cClassDescriptor *basedesc = getBaseClassDescriptor();
    int base = basedesc ? basedesc->getFieldCount() : 0;
    if (fieldName[0]=='s' && strcmp(fieldName, "senderId")==0) return base+0;
    if (fieldName[0]=='s' && strcmp(fieldName, "seq")==0) return base+1;
    if (fieldName[0]=='g' && strcmp(fieldName, "genTime")==0) return base+2;
    if (fieldName[0]=='p' && strcmp(fieldName, "positionM")==0) return base+3;
    if (fieldName[0]=='s' && strcmp(fieldName, "speedMps")==0) return base+4;
    if (fieldName[0]=='b' && strcmp(fieldName, "brakeActive")==0) return base+5;
    if (fieldName[0]=='e' && strcmp(fieldName, "eventId")==0) return base+6;
    if (fieldName[0]=='e' && strcmp(fieldName, "eventDetectTime")==0) return base+7;
    if (fieldName[0]=='e' && strcmp(fieldName, "eventValidUntil")==0) return base+8;
    return basedesc ? basedesc->findField(fieldName) : -1;
}

const char *TabrBeaconDescriptor::getFieldTypeString(int field) const
{
    omnetpp::cClassDescriptor *basedesc = getBaseClassDescriptor();
    if (basedesc) {
        if (field < basedesc->getFieldCount())
            return basedesc->getFieldTypeString(field);
        field -= basedesc->getFieldCount();
    }
    static const char *fieldTypeStrings[] = {
        "string",
        "long",
        "simtime_t",
        "double",
        "double",
        "bool",
        "long",
        "simtime_t",
        "simtime_t",
    };
    return (field>=0 && field<9) ? fieldTypeStrings[field] : nullptr;
}

const char **TabrBeaconDescriptor::getFieldPropertyNames(int field) const
{
    omnetpp::cClassDescriptor *basedesc = getBaseClassDescriptor();
    if (basedesc) {
        if (field < basedesc->getFieldCount())
            return basedesc->getFieldPropertyNames(field);
        field -= basedesc->getFieldCount();
    }
    switch (field) {
        default: return nullptr;
    }
}

const char *TabrBeaconDescriptor::getFieldProperty(int field, const char *propertyname) const
{
    omnetpp::cClassDescriptor *basedesc = getBaseClassDescriptor();
    if (basedesc) {
        if (field < basedesc->getFieldCount())
            return basedesc->getFieldProperty(field, propertyname);
        field -= basedesc->getFieldCount();
    }
    switch (field) {
        default: return nullptr;
    }
}

int TabrBeaconDescriptor::getFieldArraySize(void *object, int field) const
{
    omnetpp::cClassDescriptor *basedesc = getBaseClassDescriptor();
    if (basedesc) {
        if (field < basedesc->getFieldCount())
            return basedesc->getFieldArraySize(object, field);
        field -= basedesc->getFieldCount();
    }
    TabrBeacon *pp = (TabrBeacon *)object; (void)pp;
    switch (field) {
        default: return 0;
    }
}

const char *TabrBeaconDescriptor::getFieldDynamicTypeString(void *object, int field, int i) const
{
    omnetpp::cClassDescriptor *basedesc = getBaseClassDescriptor();
    if (basedesc) {
        if (field < basedesc->getFieldCount())
            return basedesc->getFieldDynamicTypeString(object,field,i);
        field -= basedesc->getFieldCount();
    }
    TabrBeacon *pp = (TabrBeacon *)object; (void)pp;
    switch (field) {
        default: return nullptr;
    }
}

std::string TabrBeaconDescriptor::getFieldValueAsString(void *object, int field, int i) const
{
    omnetpp::cClassDescriptor *basedesc = getBaseClassDescriptor();
    if (basedesc) {
        if (field < basedesc->getFieldCount())
            return basedesc->getFieldValueAsString(object,field,i);
        field -= basedesc->getFieldCount();
    }
    TabrBeacon *pp = (TabrBeacon *)object; (void)pp;
    switch (field) {
        case 0: return oppstring2string(pp->getSenderId());
        case 1: return long2string(pp->getSeq());
        case 2: return simtime2string(pp->getGenTime());
        case 3: return double2string(pp->getPositionM());
        case 4: return double2string(pp->getSpeedMps());
        case 5: return bool2string(pp->getBrakeActive());
        case 6: return long2string(pp->getEventId());
        case 7: return simtime2string(pp->getEventDetectTime());
        case 8: return simtime2string(pp->getEventValidUntil());
        default: return "";
    }
}

bool TabrBeaconDescriptor::setFieldValueAsString(void *object, int field, int i, const char *value) const
{
    omnetpp::cClassDescriptor *basedesc = getBaseClassDescriptor();
    if (basedesc) {
        if (field < basedesc->getFieldCount())
            return basedesc->setFieldValueAsString(object,field,i,value);
        field -= basedesc->getFieldCount();
    }
    TabrBeacon *pp = (TabrBeacon *)object; (void)pp;
    switch (field) {
        case 0: pp->setSenderId((value)); return true;
        case 1: pp->setSeq(string2long(value)); return true;
        case 2: pp->setGenTime(string2simtime(value)); return true;
        case 3: pp->setPositionM(string2double(value)); return true;
        case 4: pp->setSpeedMps(string2double(value)); return true;
        case 5: pp->setBrakeActive(string2bool(value)); return true;
        case 6: pp->setEventId(string2long(value)); return true;
        case 7: pp->setEventDetectTime(string2simtime(value)); return true;
        case 8: pp->setEventValidUntil(string2simtime(value)); return true;
        default: return false;
    }
}

const char *TabrBeaconDescriptor::getFieldStructName(int field) const
{
    omnetpp::cClassDescriptor *basedesc = getBaseClassDescriptor();
    if (basedesc) {
        if (field < basedesc->getFieldCount())
            return basedesc->getFieldStructName(field);
        field -= basedesc->getFieldCount();
    }
    switch (field) {
        default: return nullptr;
    };
}

void *TabrBeaconDescriptor::getFieldStructValuePointer(void *object, int field, int i) const
{
    omnetpp::cClassDescriptor *basedesc = getBaseClassDescriptor();
    if (basedesc) {
        if (field < basedesc->getFieldCount())
            return basedesc->getFieldStructValuePointer(object, field, i);
        field -= basedesc->getFieldCount();
    }
    TabrBeacon *pp = (TabrBeacon *)object; (void)pp;
    switch (field) {
        default: return nullptr;
    }
}

} // namespace veins

